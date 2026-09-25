"""API key manager: one-way verifiers, permissions, resource scope, quotas and audit log.

Implements the key part of F29 (MASTER-PROMPT §5) and CONTRACTS.ru.md §5.1-§5.3:
one identity per role, a secret that is shown exactly once, a one-way verifier in
the database, an object-level permission check, rate/concurrency quotas and a local
audit log that never receives a full key, a password or a response body.

Boundary with ``secrets.py``: upstream credentials that the transport must read
back live in the OS vault.  Nothing in this module accepts, stores or returns a
plaintext password, so a key can never be used as a transport credential and an
upstream credential can never be used as an API key.

Boundary with ``api.py``/``apiv1.py``: this module authenticates and authorises.
It does not serve HTTP, does not own a listener and never puts a secret into a
query string; the new managing keys travel in the ``Authorization`` header.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets as random_source
import sqlite3
import threading
import time
from collections import deque

from .i18n import tr

# --- issued secret ------------------------------------------------------------

SECRET_MARK = 'pwk_'
SECRET_BYTES = 32                      # 256 random bits behind the handle
PREFIX_LENGTH = 12                     # hex handle, fixed for the life of the key
KEY_ID_BYTES = 8
MAX_SECRET_LENGTH = 512
HANDLE_ATTEMPTS = 8                    # a handle collision is regenerated, never overwritten

# --- verifier -----------------------------------------------------------------

HASH_NAME = 'sha256'
VERIFIER_ALGO = 'pbkdf2_sha256'
PBKDF2_ITERATIONS = 210_000            # stored per row, so it can be raised later without a rewrite
SALT_BYTES = 16
# The cost of verifying an unknown prefix is deliberately the cost of a real one.
_DUMMY_ALGO = f'{VERIFIER_ALGO}${PBKDF2_ITERATIONS}'
_DUMMY_SALT = bytes(range(SALT_BYTES))
_DUMMY_DIGEST = bytes(32)

# --- limits -------------------------------------------------------------------

MAX_NAME_LENGTH = 120
MAX_PURPOSE_LENGTH = 400
DEFAULT_TOUCH_INTERVAL_S = 60.0
AUDIT_RETENTION = 5000
STREAM_RECHECK_INTERVAL_S = 30.0

# --- permissions (CONTRACTS §5.2) --------------------------------------------

READ_PERMISSIONS = frozenset({
    'read.status', 'read.results', 'read.results.detail', 'read.export.artifact',
})
WRITE_PERMISSIONS = frozenset({
    'collections.read', 'collections.write', 'import.read', 'import.commit',
    'profiles.read', 'profiles.write', 'sources.read', 'sources.write',
    'jobs.read', 'jobs.submit', 'jobs.control', 'pools.read', 'pools.write',
    'gateway.read', 'gateway.write', 'schedules.read', 'schedules.write',
    'export.create',
})
SENSITIVE_PERMISSIONS = frozenset({'export.secret'})
ADMIN_PERMISSIONS = frozenset({'admin.settings', 'admin.keys', 'admin.audit'})
PERMISSIONS = READ_PERMISSIONS | WRITE_PERMISSIONS | SENSITIVE_PERMISSIONS | ADMIN_PERMISSIONS
BOOTSTRAP_PERMISSIONS = READ_PERMISSIONS | ADMIN_PERMISSIONS

# --- error codes (CONTRACTS §5.4) ---------------------------------------------

E_MISSING = 'E_AUTH_MISSING'
E_INVALID = 'E_AUTH_INVALID'
E_EXPIRED = 'E_AUTH_EXPIRED'
E_REVOKED = 'E_AUTH_REVOKED'
E_DISABLED = 'E_AUTH_DISABLED'
E_SCOPE = 'E_AUTH_SCOPE'
E_PERMISSION = 'E_AUTH_PERMISSION'
E_RATE_LIMITED = 'E_AUTH_RATE_LIMITED'
E_ROTATION_GRACE = 'E_AUTH_ROTATION_GRACE'
E_FIELD = 'E_VALIDATION_FIELD'
E_UNKNOWN_FIELD = 'E_VALIDATION_UNKNOWN_FIELD'
E_CONCURRENCY = 'E_LIMIT_CONCURRENCY'

HTTP_STATUS = {
    E_MISSING: 401, E_INVALID: 401, E_EXPIRED: 401, E_REVOKED: 401, E_DISABLED: 401,
    E_ROTATION_GRACE: 401, E_SCOPE: 403, E_PERMISSION: 403, E_RATE_LIMITED: 429,
    E_FIELD: 400, E_UNKNOWN_FIELD: 400, E_CONCURRENCY: 429,
}

# --- redaction ----------------------------------------------------------------

REDACTED = '***'
_SECRET_RE = re.compile(r'pwk_[A-Za-z0-9_\-]{6,}')
_BEARER_RE = re.compile(r'(?i)\bbearer\s+\S+')

_UNSET = object()

# --- schema of migrations 8 and 10 --------------------------------------------

# The first fifteen columns are CONTRACTS §3.3 verbatim.  The last four are an
# additive request to db.py (see docs/integration/HANDOFF/apikeys.md): a
# reversible disable flag and the superseded verifier of a narrow rotation
# window, which cannot be expressed by the three verifier columns without
# keeping two secrets valid in one row.
API_KEYS_COLUMNS = (
    'id', 'prefix', 'name', 'purpose', 'created_at', 'expires_at', 'last_used_at', 'revoked_at',
    'permissions_json', 'resource_scope_json', 'rate_limit_json', 'concurrency_json',
    'rotation_grace_until', 'verifier', 'verifier_salt', 'verifier_algo',
    'disabled_at', 'previous_verifier', 'previous_verifier_salt', 'previous_verifier_algo',
)
CONTRACT_API_KEYS_COLUMNS = API_KEYS_COLUMNS[:16]
AUDIT_LOG_COLUMNS = ('at', 'key_id', 'operation', 'object_kind', 'object_id', 'scope_json',
                     'result', 'error_code')

_API_KEYS_DDL = '''
    CREATE TABLE IF NOT EXISTS api_keys(
        id TEXT PRIMARY KEY,
        prefix TEXT NOT NULL,
        name TEXT,
        purpose TEXT,
        created_at REAL,
        expires_at REAL,
        last_used_at REAL,
        revoked_at REAL,
        permissions_json TEXT NOT NULL,
        resource_scope_json TEXT NOT NULL,
        rate_limit_json TEXT,
        concurrency_json TEXT,
        rotation_grace_until REAL,
        verifier TEXT NOT NULL,
        verifier_salt TEXT NOT NULL,
        verifier_algo TEXT NOT NULL,
        disabled_at REAL,
        previous_verifier TEXT,
        previous_verifier_salt TEXT,
        previous_verifier_algo TEXT
    );
'''
_AUDIT_LOG_DDL = '''
    CREATE TABLE IF NOT EXISTS audit_log(
        at REAL NOT NULL,
        key_id TEXT,
        operation TEXT NOT NULL,
        object_kind TEXT,
        object_id TEXT,
        scope_json TEXT,
        result TEXT,
        error_code TEXT
    );
'''
_API_KEYS_COLUMNS_DDL = {
    'disabled_at': 'ALTER TABLE api_keys ADD COLUMN disabled_at REAL',
    'previous_verifier': 'ALTER TABLE api_keys ADD COLUMN previous_verifier TEXT',
    'previous_verifier_salt': 'ALTER TABLE api_keys ADD COLUMN previous_verifier_salt TEXT',
    'previous_verifier_algo': 'ALTER TABLE api_keys ADD COLUMN previous_verifier_algo TEXT',
}


def ensure_schema(conn):
    """Create the two tables of migrations 8 and 10 and the index of migration 14.

    ``db.py`` owns the DDL of the project.  This function exists so that the
    executable text of these two migrations has exactly one representation, here,
    and so that this module can be exercised before the migrator lands.  It is
    idempotent: on a database already migrated by ``db.migrate()`` it changes
    nothing, and a column that is present is never added twice.
    """
    conn.executescript(_API_KEYS_DDL + _AUDIT_LOG_DDL)
    present = {row[1] for row in conn.execute('PRAGMA table_info(api_keys)')}
    for column, statement in _API_KEYS_COLUMNS_DDL.items():
        if column not in present:
            conn.execute(statement)
    conn.execute('CREATE INDEX IF NOT EXISTS api_keys_prefix ON api_keys(prefix)')
    conn.execute('CREATE INDEX IF NOT EXISTS audit_log_at ON audit_log(at)')
    conn.commit()


class ApiKeyError(Exception):
    """A refusal that carries a machine code from CONTRACTS §5.4 and a next action."""

    def __init__(self, code, *, detail=None, action=None, retry_after=None, state=None):
        self.code = code
        self.detail = detail
        self.action = action
        self.retry_after = retry_after
        self.state = state
        super().__init__(f'{code}: {detail or action or ""}'.strip(': '))

    @property
    def http_status(self):
        return HTTP_STATUS.get(self.code, 400)

    def as_json(self):
        body = {'error': {'code': self.code}}
        if self.detail:
            body['error']['detail'] = self.detail
        if self.action:
            body['error']['action'] = self.action
        if self.retry_after is not None:
            body['error']['retry_after'] = round(float(self.retry_after), 3)
        if self.state:
            body['error']['state'] = self.state
        return body


def _b64encode(raw):
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def _b64decode(text):
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def _dump(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def _load(text, fallback):
    if not text:
        return fallback
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return fallback


def redact(value):
    """Mask anything shaped like an issued key or a bearer token."""
    if not isinstance(value, str) or not value:
        return value
    value = _BEARER_RE.sub(f'Bearer {REDACTED}', value)
    keep = len(SECRET_MARK) + PREFIX_LENGTH
    return _SECRET_RE.sub(lambda match: match.group(0)[:keep] + REDACTED, value)


def secret_prefix(secret):
    """The public handle of a presented secret, or None when it is not one of ours."""
    if not isinstance(secret, str) or not secret.startswith(SECRET_MARK):
        return None
    prefix, separator, body = secret[len(SECRET_MARK):].partition('_')
    if not separator or not body or not prefix:
        return None
    return prefix


def _redact_all(values):
    if isinstance(values, str):
        return redact(values)
    if isinstance(values, (list, tuple)):
        return [_redact_all(item) for item in values]
    if isinstance(values, dict):
        return {key: _redact_all(item) for key, item in values.items()}
    return values


def _text(value, field, max_length):
    if not isinstance(value, str) or not value.strip():
        raise ApiKeyError(E_FIELD, detail=f'{field} is required',
                          action=f'pass a non-empty {field}')
    if len(value) > max_length:
        raise ApiKeyError(E_FIELD, detail=f'{field} is longer than {max_length} characters',
                          action=f'shorten {field}')
    return value.strip()


def _optional_text(value, field, max_length):
    if value is None:
        return None
    return _text(value, field, max_length)


def _deadline(expires_at, ttl_s, now):
    if expires_at is not None and ttl_s is not None:
        raise ApiKeyError(E_FIELD, detail='expires_at and ttl_s are mutually exclusive',
                          action='pass either expires_at or ttl_s')
    if ttl_s is not None:
        if not isinstance(ttl_s, (int, float)) or isinstance(ttl_s, bool) or ttl_s <= 0:
            raise ApiKeyError(E_FIELD, detail='ttl_s must be a positive number',
                              action='pass ttl_s > 0 or omit it')
        return float(now) + float(ttl_s)
    if expires_at is not None:
        expires_at = float(expires_at)
        if expires_at <= now:
            raise ApiKeyError(E_FIELD, detail='expires_at is already in the past',
                              action='pass a future expires_at or omit it')
        return expires_at
    return None


def _hash_secret(secret, salt, iterations):
    return hashlib.pbkdf2_hmac(HASH_NAME, secret.encode('utf-8'), salt, int(iterations))


def _decode_or_empty(text):
    try:
        return _b64decode(text or '')
    except (TypeError, ValueError):
        return b''


def _verify_digest(secret, algo, salt, digest):
    """Constant-time check of one verifier record."""
    name, _, count = (algo or '').partition('$')
    if name != VERIFIER_ALGO or not salt or not digest:
        return False
    try:
        computed = _hash_secret(secret, salt, count)
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(computed, _decode_or_empty(digest))


class Scope:
    """Which collections and pools a key may touch. Empty means 'whatever its rights allow'."""

    __slots__ = ('collections', 'pools')

    def __init__(self, collections=(), pools=()):
        self.collections = tuple(collections)
        self.pools = tuple(pools)

    @classmethod
    def of(cls, value):
        if value is None:
            return cls()
        if isinstance(value, Scope):
            return value
        if not isinstance(value, dict):
            raise ApiKeyError(E_FIELD, detail='scope must be a mapping',
                              action='pass {"collections": [...], "pools": [...]}')
        unknown = set(value) - {'collections', 'pools'}
        if unknown:
            raise ApiKeyError(E_UNKNOWN_FIELD, detail=f'unknown scope fields: {sorted(unknown)}',
                              action='use only collections and pools')
        return cls(_id_list(value.get('collections'), 'collections'),
                   _id_list(value.get('pools'), 'pools'))

    @property
    def unrestricted(self):
        return not self.collections and not self.pools

    def allows_collection(self, collection_id):
        return not self.collections or collection_id in self.collections

    def allows_pool(self, pool_id):
        return not self.pools or pool_id in self.pools

    def as_json(self):
        return {'collections': list(self.collections), 'pools': list(self.pools)}

    def __eq__(self, other):
        return isinstance(other, Scope) and self.as_json() == other.as_json()

    def __repr__(self):
        return f'Scope(collections={self.collections!r}, pools={self.pools!r})'


def _id_list(value, field):
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, (list, tuple, set, frozenset)):
        raise ApiKeyError(E_FIELD, detail=f'scope.{field} must be a list of ids',
                          action=f'pass {field} as a list of strings')
    ids = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ApiKeyError(E_FIELD, detail=f'scope.{field} holds an empty id',
                              action=f'pass {field} as a list of non-empty strings')
        if item not in ids:
            ids.append(item)
    return tuple(ids)


class RateLimit:
    """Requests allowed inside a sliding window."""

    __slots__ = ('requests', 'window_s')

    def __init__(self, requests, window_s):
        if not isinstance(requests, int) or isinstance(requests, bool) or requests < 1:
            raise ApiKeyError(E_FIELD, detail='rate_limit.requests must be an integer >= 1',
                              action='pass a positive request count')
        if not isinstance(window_s, (int, float)) or isinstance(window_s, bool) or window_s <= 0:
            raise ApiKeyError(E_FIELD, detail='rate_limit.window_s must be a positive number',
                              action='pass a positive window in seconds')
        self.requests = int(requests)
        self.window_s = float(window_s)

    @classmethod
    def of(cls, value):
        if value is None or isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise ApiKeyError(E_FIELD, detail='rate_limit must be a mapping',
                              action='pass {"requests": N, "window_s": S}')
        unknown = set(value) - {'requests', 'window_s'}
        if unknown:
            raise ApiKeyError(E_UNKNOWN_FIELD, detail=f'unknown rate_limit fields: {sorted(unknown)}',
                              action='use only requests and window_s')
        if value.get('requests') is None or value.get('window_s') is None:
            raise ApiKeyError(E_FIELD, detail='rate_limit needs requests and window_s',
                              action='pass both requests and window_s')
        return cls(value['requests'], value['window_s'])

    def as_json(self):
        return {'requests': self.requests, 'window_s': self.window_s}

    def __eq__(self, other):
        return isinstance(other, RateLimit) and self.as_json() == other.as_json()

    def __repr__(self):
        return f'RateLimit({self.requests}/{self.window_s}s)'


class Concurrency:
    """How many requests, subscriptions and leases one key may hold at once."""

    __slots__ = ('max_active',)

    def __init__(self, max_active):
        if not isinstance(max_active, int) or isinstance(max_active, bool) or max_active < 1:
            raise ApiKeyError(E_FIELD, detail='concurrency.max_active must be an integer >= 1',
                              action='pass a positive slot count')
        self.max_active = int(max_active)

    @classmethod
    def of(cls, value):
        if value is None or isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise ApiKeyError(E_FIELD, detail='concurrency must be a mapping',
                              action='pass {"max_active": N}')
        unknown = set(value) - {'max_active'}
        if unknown:
            raise ApiKeyError(E_UNKNOWN_FIELD, detail=f'unknown concurrency fields: {sorted(unknown)}',
                              action='use only max_active')
        if value.get('max_active') is None:
            raise ApiKeyError(E_FIELD, detail='concurrency needs max_active',
                              action='pass max_active')
        return cls(value['max_active'])

    def as_json(self):
        return {'max_active': self.max_active}

    def __eq__(self, other):
        return isinstance(other, Concurrency) and self.max_active == other.max_active

    def __repr__(self):
        return f'Concurrency({self.max_active})'


class KeyInfo:
    """Everything a list or a settings screen may show. Never carries a verifier."""

    __slots__ = ('id', 'prefix', 'name', 'purpose', 'created_at', 'expires_at', 'last_used_at',
                 'revoked_at', 'disabled_at', 'state', 'permissions', 'scope', 'rate_limit',
                 'concurrency', 'rotation_grace_until')

    def __init__(self, row, now, *, effective_last_used_at=None):
        self.id = row['id']
        self.prefix = row['prefix']
        self.name = row['name']
        self.purpose = row['purpose']
        self.created_at = row['created_at']
        self.expires_at = row['expires_at']
        self.last_used_at = (effective_last_used_at
                             if effective_last_used_at is not None else row['last_used_at'])
        self.revoked_at = row['revoked_at']
        self.disabled_at = row['disabled_at']
        self.state = self.state_of(row, now)
        self.permissions = tuple(sorted(_load(row['permissions_json'], [])))
        self.scope = Scope.of(_load(row['resource_scope_json'], {}))
        self.rate_limit = RateLimit.of(_load(row['rate_limit_json'], None))
        self.concurrency = Concurrency.of(_load(row['concurrency_json'], None))
        self.rotation_grace_until = row['rotation_grace_until']

    @staticmethod
    def state_of(row, now):
        if row['revoked_at'] is not None:
            return 'revoked'
        if row['disabled_at'] is not None:
            return 'disabled'
        if row['expires_at'] is not None and row['expires_at'] <= now:
            return 'expired'
        return 'active'

    @property
    def active(self):
        return self.state == 'active'

    def expires_in_s(self, now):
        if self.expires_at is None:
            return None
        return round(self.expires_at - now, 3)

    def as_dict(self, now=None):
        return {
            'id': self.id, 'prefix': self.prefix, 'name': self.name, 'purpose': self.purpose,
            'created_at': self.created_at, 'expires_at': self.expires_at,
            'expires_in_s': self.expires_in_s(now) if now is not None else None,
            'last_used_at': self.last_used_at, 'state': self.state,
            'revoked_at': self.revoked_at, 'disabled_at': self.disabled_at,
            'permissions': list(self.permissions), 'scope': self.scope.as_json(),
            'rate_limit': self.rate_limit.as_json() if self.rate_limit else None,
            'concurrency': self.concurrency.as_json() if self.concurrency else None,
            'rotation_grace_until': self.rotation_grace_until,
        }

    def __repr__(self):
        return f'KeyInfo(id={self.id!r}, prefix={self.prefix!r}, state={self.state!r})'


class IssuedKey:
    """The one and only moment a full secret exists outside the caller's memory."""

    __slots__ = ('info', 'secret', 'warnings')

    def __init__(self, info, secret, warnings):
        self.info = info
        self.secret = secret
        self.warnings = tuple(warnings)

    @property
    def key_id(self):
        return self.info.id

    def as_json(self):
        """For a CLI or a GUI dialog that must show the secret exactly once."""
        body = self.info.as_dict()
        body['secret'] = self.secret
        body['warnings'] = list(self.warnings)
        return body

    def __repr__(self):
        return f'IssuedKey(key_id={self.info.id!r}, prefix={self.info.prefix!r})'


class Decision:
    """Result of one object-level permission check."""

    __slots__ = ('allowed', 'code', 'permission', 'include_secrets', 'collection_id', 'pool_id',
                 'state')

    def __init__(self, allowed, code, permission, *, include_secrets=False, collection_id=None,
                 pool_id=None, state=None):
        self.allowed = allowed
        self.code = code
        self.permission = permission
        self.include_secrets = include_secrets
        self.collection_id = collection_id
        self.pool_id = pool_id
        self.state = state

    def __bool__(self):
        return self.allowed

    @property
    def http_status(self):
        return HTTP_STATUS.get(self.code, 400)

    def raise_if_denied(self):
        if not self.allowed:
            raise ApiKeyError(self.code, detail=f'key is not allowed to {self.permission}',
                              action='ask an administrator for the missing right',
                              state=self.state)
        return self

    def __repr__(self):
        return f'Decision(allowed={self.allowed}, permission={self.permission!r}, code={self.code!r})'


class Principal:
    """The identity behind one request, built by :meth:`ApiKeyManager.authenticate`."""

    __slots__ = ('info', 'in_grace', 'code')

    def __init__(self, info, *, in_grace=False, code=None):
        self.info = info
        self.in_grace = bool(in_grace)
        self.code = code

    @property
    def key_id(self):
        return self.info.id

    @property
    def prefix(self):
        return self.info.prefix

    @property
    def state(self):
        return self.info.state

    @property
    def scope(self):
        return self.info.scope

    @property
    def rate_limit(self):
        return self.info.rate_limit

    @property
    def concurrency(self):
        return self.info.concurrency

    def has(self, permission):
        return permission in self.info.permissions

    def require(self, permission, *, include_secrets=False, collection_id=None, pool_id=None):
        return authorize(self, permission, include_secrets=include_secrets,
                         collection_id=collection_id, pool_id=pool_id)

    def as_dict(self):
        body = self.info.as_dict()
        body['in_grace'] = self.in_grace
        return body

    def __repr__(self):
        return f'Principal(key_id={self.key_id!r}, state={self.state!r}, in_grace={self.in_grace})'


def visible_collections(principal):
    """Collections a key may see, or None when its rights already limit nothing."""
    return None if principal.scope.unrestricted else list(principal.scope.collections)


def visible_pools(principal):
    return None if principal.scope.unrestricted else list(principal.scope.pools)


def object_visible(principal, kind, object_id, *, collection_id=None, pool_id=None):
    """True only for an object inside the key's own scope.

    A foreign object and a missing object both answer False, so a client cannot
    tell the difference between them (CONTRACTS §5.3).  An object that belongs to
    a collection or a pool — a result row, an export artifact, a job, a batch, a
    stream — is visible only when the key is unscoped or when the object names
    its own collection and that collection is in scope.  An object whose owner
    cannot be named stays invisible, so a job id alone never widens anything.
    """
    if kind == 'collection':
        return principal.scope.allows_collection(object_id)
    if kind == 'pool':
        return principal.scope.allows_pool(pool_id if pool_id is not None else object_id)
    if kind not in ('result', 'artifact', 'job', 'batch', 'stream'):
        return False
    if principal.scope.unrestricted:
        return True
    if collection_id is None or not principal.scope.allows_collection(collection_id):
        return False
    if pool_id is not None and not principal.scope.allows_pool(pool_id):
        return False
    return True


def effective_collections(principal, requested):
    """Intersection of a requested collection list with the key's own scope.

    Filters narrow the visible set, they never widen it: asking for a foreign
    collection is refused instead of answered.
    """
    if requested is None:
        return visible_collections(principal)
    if isinstance(requested, str):
        requested = [requested]
    requested = _id_list(requested, 'collection_id')
    out = []
    for collection_id in requested:
        if not principal.scope.allows_collection(collection_id):
            raise ApiKeyError(E_SCOPE, detail='collection is outside the key scope',
                              action='use a collection this key is scoped to')
        if collection_id not in out:
            out.append(collection_id)
    return out


def authorize(principal, permission, *, include_secrets=False, collection_id=None, pool_id=None):
    """Object-level check for one operation.

    ``include_secrets=True`` is the explicit parameter of a secret-bearing export
    and needs ``export.secret`` in addition to ``export.create``.
    """
    if permission not in PERMISSIONS:
        raise ApiKeyError(E_UNKNOWN_FIELD, detail=f'unknown permission {permission!r}',
                          action='use a permission from the documented set')
    state = principal.state
    if principal.has(permission) and not (include_secrets and not principal.has('export.secret')):
        if collection_id is not None and not principal.scope.allows_collection(collection_id):
            return Decision(False, E_SCOPE, permission, include_secrets=include_secrets,
                            collection_id=collection_id, pool_id=pool_id, state=state)
        if pool_id is not None and not principal.scope.allows_pool(pool_id):
            return Decision(False, E_SCOPE, permission, include_secrets=include_secrets,
                            collection_id=collection_id, pool_id=pool_id, state=state)
        return Decision(True, None, permission, include_secrets=include_secrets,
                        collection_id=collection_id, pool_id=pool_id, state=state)
    return Decision(False, E_PERMISSION, permission, include_secrets=include_secrets,
                    collection_id=collection_id, pool_id=pool_id, state=state)


class ApiKeyManager:
    """Lifecycle, authentication and audit for API keys.

    The connection belongs to the caller.  Every database access goes through one
    lock, so the manager is safe to share between the threads of a local HTTP
    server — but a ``sqlite3`` connection is bound to the thread that opened it,
    so a server that shares one connection must open it with
    ``check_same_thread=False`` (or give every thread its own manager and its own
    connection).  ``now`` is injectable so that expiry and grace windows are
    testable without sleeping.
    """

    def __init__(self, conn, *, now=time.time, iterations=PBKDF2_ITERATIONS,
                 touch_interval_s=DEFAULT_TOUCH_INTERVAL_S, audit_retention=AUDIT_RETENTION):
        self.conn = conn
        self._now = now
        self.iterations = int(iterations)
        self.touch_interval_s = float(touch_interval_s)
        self.audit_retention = int(audit_retention)
        self._lock = threading.RLock()
        self._touched = {}
        conn.row_factory = sqlite3.Row
        self._columns = {row[1] for row in conn.execute('PRAGMA table_info(api_keys)')}
        if 'id' not in self._columns:
            raise ApiKeyError(E_INVALID, detail='table api_keys is missing',
                              action='run the migrations of CONTRACTS §3.3 before using keys')

    # -- clock ----------------------------------------------------------------

    def now(self):
        return float(self._now())

    def _time(self, override=None):
        return self.now() if override is None else float(override)

    # -- issue ----------------------------------------------------------------

    def bootstrap_admin(self, *, local_trusted, name='administrator', purpose=None,
                        expires_at=None, ttl_s=None, permissions=None, scope=None,
                        rate_limit=None, concurrency=None):
        """Mint the first administrative key, and only from a local trusted surface.

        ``local_trusted`` must be set by the caller that already knows its own
        transport is the local GUI or the local CLI; the network surface never
        sets it.  Every other key comes from :meth:`create` with an actor that
        already holds ``admin.keys``, so a read-only key can never promote itself.
        """
        if not local_trusted:
            raise ApiKeyError(E_PERMISSION, detail='admin bootstrap is a local-only operation',
                              action='run it from the local GUI or the local CLI',
                              state='not_local')
        granted = BOOTSTRAP_PERMISSIONS if permissions is None else permissions
        issued = self._issue(name=name, purpose=purpose, expires_at=expires_at, ttl_s=ttl_s,
                             permissions=granted, scope=scope, rate_limit=rate_limit,
                             concurrency=concurrency)
        self._audit(None, 'key.bootstrap', object_kind='api_key', object_id=issued.key_id,
                    scope=scope, result='ok')
        return issued

    def create(self, *, actor, name, purpose=None, expires_at=None, ttl_s=None, permissions=(),
               scope=None, rate_limit=None, concurrency=None):
        """Create a key on behalf of an actor that holds ``admin.keys``."""
        principal = self._as_principal(actor)
        principal.require('admin.keys').raise_if_denied()
        issued = self._issue(name=name, purpose=purpose, expires_at=expires_at, ttl_s=ttl_s,
                             permissions=permissions, scope=scope, rate_limit=rate_limit,
                             concurrency=concurrency)
        self._audit(principal.key_id, 'key.create', object_kind='api_key',
                    object_id=issued.key_id, scope=scope, result='ok')
        return issued

    def _issue(self, *, name, purpose, expires_at, ttl_s, permissions, scope, rate_limit,
               concurrency):
        now = self.now()
        name = _text(name, 'name', MAX_NAME_LENGTH)
        purpose = _optional_text(purpose, 'purpose', MAX_PURPOSE_LENGTH)
        deadline = _deadline(expires_at, ttl_s, now)
        granted = _permissions(permissions)
        parsed_scope = Scope.of(scope)
        rate = RateLimit.of(rate_limit)
        slots = Concurrency.of(concurrency)
        secret, verifier, salt, algo = self._new_secret()
        key_id = random_source.token_hex(KEY_ID_BYTES)
        handle = secret[len(SECRET_MARK):].partition('_')[0]
        row = (
            ('id', key_id), ('prefix', handle), ('name', name), ('purpose', purpose),
            ('created_at', now), ('expires_at', deadline), ('last_used_at', None),
            ('revoked_at', None), ('permissions_json', _dump(sorted(granted))),
            ('resource_scope_json', _dump(parsed_scope.as_json())),
            ('rate_limit_json', _dump(rate.as_json()) if rate else None),
            ('concurrency_json', _dump(slots.as_json()) if slots else None),
            ('rotation_grace_until', None), ('verifier', verifier), ('verifier_salt', salt),
            ('verifier_algo', algo), ('disabled_at', None), ('previous_verifier', None),
            ('previous_verifier_salt', None), ('previous_verifier_algo', None),
        )
        with self._lock:
            self.conn.execute(*self._insert(row))
            self.conn.commit()
        info = KeyInfo(self._row(key_id), now)
        warnings = [tr('Секрет показывается один раз и нигде не сохраняется — сохраните его сейчас.',
                       'the secret is shown once and never stored — save it now')]
        if not granted:
            warnings.append(tr('Без прав ключ не может ничего; это не ошибка, а пустая выдача.',
                               'without rights the key can do nothing; that is an empty grant, not a fault'))
        if 'export.secret' in granted:
            warnings.append(tr('Ключ умеет отдавать экспорт с credentials — держите его как секрет.',
                               'this key can export credentials — treat it as a secret'))
        return IssuedKey(info, secret, warnings)

    def _new_secret(self, handle=None):
        body = _b64encode(random_source.token_bytes(SECRET_BYTES))
        if handle is None:
            handle = self._new_handle()
        secret = f'{SECRET_MARK}{handle}_{body}'
        salt = random_source.token_bytes(SALT_BYTES)
        algo = f'{VERIFIER_ALGO}${self.iterations}'
        return secret, _b64encode(_hash_secret(secret, salt, self.iterations)), _b64encode(salt), algo

    def _new_handle(self):
        """A public hex handle. Hex only: the handle must not contain the '_' separator."""
        for _ in range(HANDLE_ATTEMPTS):
            handle = random_source.token_hex(PREFIX_LENGTH // 2)
            if self._row_by_prefix(handle) is None:
                return handle
        raise ApiKeyError(E_FIELD, detail='could not find a free key handle',
                          action='try again')

    # -- lifecycle ------------------------------------------------------------

    def list_keys(self, *, actor, state=None, limit=500):
        principal = self._as_principal(actor)
        principal.require('admin.keys').raise_if_denied()
        now = self.now()
        with self._lock:
            rows = self.conn.execute('SELECT * FROM api_keys ORDER BY created_at, id').fetchall()
        infos = [KeyInfo(record, now, effective_last_used_at=self._touched.get(record['id'],
                                                                                record['last_used_at']))
                 for record in (self._as_record(row) for row in rows)]
        if state is not None:
            wanted = {state} if isinstance(state, str) else set(state)
            infos = [info for info in infos if info.state in wanted]
        return infos[:max(0, int(limit))]

    def get_key(self, key_id, *, actor=None):
        if actor is not None:
            self._as_principal(actor).require('admin.keys').raise_if_denied()
        row = self._row(key_id)
        if row is None:
            raise ApiKeyError(E_FIELD, detail='unknown key id', action='list the keys again')
        return KeyInfo(row, self.now(),
                       effective_last_used_at=self._touched.get(key_id, row['last_used_at']))

    def update_metadata(self, key_id, *, actor, name=_UNSET, purpose=_UNSET, expires_at=_UNSET,
                        ttl_s=_UNSET):
        """Rename or re-document a key. Rights, scope and quotas are not editable here."""
        principal = self._as_principal(actor)
        principal.require('admin.keys').raise_if_denied()
        row = self._row(key_id)
        if row is None:
            raise ApiKeyError(E_FIELD, detail='unknown key id', action='list the keys again')
        if row['revoked_at'] is not None:
            raise ApiKeyError(E_REVOKED, detail='a revoked key is not editable',
                              action='create a new key instead', state='revoked')
        now = self.now()
        fields = []
        if name is not _UNSET:
            fields.append(('name', _text(name, 'name', MAX_NAME_LENGTH)))
        if purpose is not _UNSET:
            fields.append(('purpose', _optional_text(purpose, 'purpose', MAX_PURPOSE_LENGTH)))
        if expires_at is not _UNSET or ttl_s is not _UNSET:
            fields.append(('expires_at', _deadline(None if expires_at is _UNSET else expires_at,
                                                   None if ttl_s is _UNSET else ttl_s, now)))
        with self._lock:
            self.conn.execute(*self._update(key_id, fields))
            self.conn.commit()
        self._audit(principal.key_id, 'key.update_metadata', object_kind='api_key',
                    object_id=key_id, result='ok')
        return self.get_key(key_id)

    def disable(self, key_id, *, actor):
        """Stop use of the key without destroying it; :meth:`enable` brings it back."""
        principal = self._as_principal(actor)
        principal.require('admin.keys').raise_if_denied()
        self._require_columns('disabled_at')
        row = self._row(key_id)
        if row is None:
            raise ApiKeyError(E_FIELD, detail='unknown key id', action='list the keys again')
        if row['revoked_at'] is not None:
            raise ApiKeyError(E_REVOKED, detail='a revoked key cannot be disabled',
                              action='create a new key instead', state='revoked')
        now = self.now()
        with self._lock:
            self.conn.execute(*self._update(key_id, [('disabled_at', now)],
                                           require=('disabled_at',)))
            self.conn.commit()
        self._audit(principal.key_id, 'key.disable', object_kind='api_key', object_id=key_id,
                    result='ok')
        return self.get_key(key_id)

    def enable(self, key_id, *, actor):
        principal = self._as_principal(actor)
        principal.require('admin.keys').raise_if_denied()
        row = self._row(key_id)
        if row is None:
            raise ApiKeyError(E_FIELD, detail='unknown key id', action='list the keys again')
        if row['revoked_at'] is not None:
            raise ApiKeyError(E_REVOKED, detail='a revoked key cannot be re-enabled',
                              action='create a new key instead', state='revoked')
        with self._lock:
            self.conn.execute(*self._update(key_id, [('disabled_at', None)],
                                           require=('disabled_at',)))
            self.conn.commit()
        self._audit(principal.key_id, 'key.enable', object_kind='api_key', object_id=key_id,
                    result='ok')
        return self.get_key(key_id)

    def revoke(self, key_id, *, actor):
        """Terminal: a revoked key keeps its metadata and its audit trail, never its use."""
        principal = self._as_principal(actor)
        principal.require('admin.keys').raise_if_denied()
        row = self._row(key_id)
        if row is None:
            raise ApiKeyError(E_FIELD, detail='unknown key id', action='list the keys again')
        now = self.now()
        with self._lock:
            revoked = self._row(key_id)['revoked_at'] or now
            self.conn.execute(*self._update(key_id, [
                ('revoked_at', revoked), ('disabled_at', None),
                ('rotation_grace_until', None), ('previous_verifier', None),
                ('previous_verifier_salt', None), ('previous_verifier_algo', None)]))
            self.conn.commit()
        self._audit(principal.key_id, 'key.revoke', object_kind='api_key', object_id=key_id,
                    result='ok')
        return self.get_key(key_id)

    def delete(self, key_id, *, actor):
        """Remove the row. The audit log keeps the history of the deleted key."""
        principal = self._as_principal(actor)
        principal.require('admin.keys').raise_if_denied()
        row = self._row(key_id)
        if row is None:
            raise ApiKeyError(E_FIELD, detail='unknown key id', action='list the keys again')
        with self._lock:
            self.conn.execute('DELETE FROM api_keys WHERE id = ?', (key_id,))
            self.conn.commit()
        self._touched.pop(key_id, None)
        self._audit(principal.key_id, 'key.delete', object_kind='api_key', object_id=key_id,
                    result='ok')
        return None

    def rotate(self, key_id, *, actor, grace_s=0.0):
        """Issue a new secret for the same key id and metadata.

        With ``grace_s > 0`` the superseded secret keeps working for exactly that
        narrow window; the window is a per-key setting, never a default.
        """
        principal = self._as_principal(actor)
        principal.require('admin.keys').raise_if_denied()
        self._require_columns('previous_verifier', 'previous_verifier_salt',
                              'previous_verifier_algo')
        row = self._row(key_id)
        if row is None:
            raise ApiKeyError(E_FIELD, detail='unknown key id', action='list the keys again')
        if row['revoked_at'] is not None:
            raise ApiKeyError(E_REVOKED, detail='a revoked key cannot be rotated',
                              action='create a new key instead', state='revoked')
        if grace_s is not None and (not isinstance(grace_s, (int, float)) or isinstance(grace_s, bool)
                                    or grace_s < 0):
            raise ApiKeyError(E_FIELD, detail='grace_s must be a number >= 0',
                              action='pass grace_s = 0 to end the old secret at once')
        now = self.now()
        # The public handle belongs to the key, not to the secret, so the superseded
        # secret stays findable by prefix for the whole rotation window.
        secret, verifier, salt, algo = self._new_secret(row['prefix'])
        if grace_s:
            grace_until = now + float(grace_s)
            previous = (row['verifier'], row['verifier_salt'], row['verifier_algo'])
        else:
            grace_until, previous = None, (None, None, None)
        with self._lock:
            self.conn.execute(*self._update(key_id, [
                ('verifier', verifier), ('verifier_salt', salt), ('verifier_algo', algo),
                ('rotation_grace_until', grace_until), ('previous_verifier', previous[0]),
                ('previous_verifier_salt', previous[1]), ('previous_verifier_algo', previous[2])],
                require=('previous_verifier', 'previous_verifier_salt',
                         'previous_verifier_algo')))
            self.conn.commit()
        self._audit(principal.key_id, 'key.rotate', object_kind='api_key', object_id=key_id,
                    result='ok', scope={'grace_s': float(grace_s or 0.0)})
        info = self.get_key(key_id)
        warnings = [tr('Новый секрет показывается один раз.',
                       'the new secret is shown once')]
        if grace_s:
            warnings.append(tr(f'Старый секрет перестаёт работать через {int(grace_s)} с.',
                               f'the superseded secret stops working in {int(grace_s)} s'))
        return IssuedKey(info, secret, warnings)

    def purge_expired_grace(self, now=None):
        """Drop the superseded verifier of windows that have closed."""
        self._require_columns('previous_verifier', 'previous_verifier_salt',
                              'previous_verifier_algo')
        moment = self._time(now)
        with self._lock:
            cursor = self.conn.execute(
                'UPDATE api_keys SET previous_verifier = NULL, previous_verifier_salt = NULL,'
                ' previous_verifier_algo = NULL, rotation_grace_until = NULL'
                ' WHERE rotation_grace_until IS NOT NULL AND rotation_grace_until <= ?', (moment,))
            self.conn.commit()
            return cursor.rowcount

    # -- authentication -------------------------------------------------------

    def authenticate(self, secret, *, permission=None, include_secrets=False, collection_id=None,
                     pool_id=None, allow_grace=False, touch=True, now=None):
        """Verify a presented secret and, optionally, one operation it wants to perform.

        Revocation, disable and expiry are read on every call, so a key that was
        revoked a moment ago cannot use anything opened before that moment.
        """
        moment = self._time(now)
        if not secret or not isinstance(secret, str):
            raise ApiKeyError(E_MISSING, detail='no key presented',
                              action='send Authorization: Bearer <key>')
        if len(secret) > MAX_SECRET_LENGTH:
            self._reject(secret, E_INVALID)
            raise ApiKeyError(E_INVALID, detail='key is not recognised',
                              action='check the prefix and copy the key whole')
        handle = secret_prefix(secret)
        if handle is None:
            self._reject(secret, E_INVALID)
            raise ApiKeyError(E_INVALID, detail='key is not recognised',
                              action='check the prefix and copy the key whole')
        row = self._row_by_prefix(handle)
        in_grace = False
        if row is None:
            # Same work as a real check, so an unknown prefix costs what a known one costs.
            self._match_unknown_prefix(secret)
            self._audit(None, 'authenticate', result='denied', error_code=E_INVALID)
            raise ApiKeyError(E_INVALID, detail='key is not recognised',
                              action='check the prefix and copy the key whole')
        if self._match(row, secret):
            pass
        elif self._match_previous(row, secret, moment):
            in_grace = True
        else:
            self._audit(None, 'authenticate', result='denied', error_code=E_INVALID)
            raise ApiKeyError(E_INVALID, detail='key is not recognised',
                              action='check the prefix and copy the key whole')
        info = KeyInfo(row, moment)
        self._assert_usable(info, operation='authenticate')
        if in_grace and not allow_grace:
            raise ApiKeyError(E_ROTATION_GRACE,
                              detail='this secret is superseded but still inside the rotation window',
                              action='move the client to the new secret', state=info.state)
        principal = Principal(info, in_grace=in_grace,
                              code=E_ROTATION_GRACE if in_grace else None)
        if permission is not None:
            authorize(principal, permission, include_secrets=include_secrets,
                      collection_id=collection_id, pool_id=pool_id).raise_if_denied()
        if touch:
            self._touch(row, moment)
        return principal

    def assert_active(self, key_id, *, operation='request', now=None):
        """Re-read the state of a key. Used by leases and by open event streams."""
        moment = self._time(now)
        row = self._row(key_id)
        if row is None:
            raise ApiKeyError(E_INVALID, detail='key is not recognised',
                              action='the key was deleted; issue a new one', state='deleted')
        self._assert_usable(KeyInfo(row, moment), operation=operation)
        return KeyInfo(row, moment, effective_last_used_at=self._touched.get(key_id, row['last_used_at']))

    def _assert_usable(self, info, *, operation):
        if info.state == 'revoked':
            self._audit(info.id, operation, object_kind='api_key', object_id=info.id,
                        result='denied', error_code=E_REVOKED)
            raise ApiKeyError(E_REVOKED, detail='key was revoked', state=info.state,
                              action='ask the administrator for a new key')
        if info.state == 'disabled':
            self._audit(info.id, operation, object_kind='api_key', object_id=info.id,
                        result='denied', error_code=E_DISABLED)
            raise ApiKeyError(E_DISABLED, detail='key is disabled', state=info.state,
                              action='ask the administrator to enable it')
        if info.state == 'expired':
            self._audit(info.id, operation, object_kind='api_key', object_id=info.id,
                        result='denied', error_code=E_EXPIRED)
            raise ApiKeyError(E_EXPIRED, detail='key has expired', state=info.state,
                              action='ask the administrator for a new key')

    def _match(self, row, secret):
        return _verify_digest(secret, row['verifier_algo'], _decode_or_empty(row['verifier_salt']),
                              row['verifier'])

    def _match_previous(self, row, secret, moment):
        if not row['previous_verifier'] or row['rotation_grace_until'] is None:
            return False
        if moment > row['rotation_grace_until']:
            return False
        return _verify_digest(secret, row['previous_verifier_algo'],
                              _decode_or_empty(row['previous_verifier_salt']),
                              row['previous_verifier'])

    def _match_unknown_prefix(self, secret):
        """Same work as a real check, for a prefix that is not in the table."""
        return _verify_digest(secret, _DUMMY_ALGO, _DUMMY_SALT, _DUMMY_DIGEST)

    def _reject(self, secret, code):
        self._match_unknown_prefix(secret)
        self._audit(None, 'authenticate', result='denied', error_code=code)

    def _touch(self, row, moment):
        key_id = row['id']
        last = self._touched.get(key_id, row['last_used_at'] or 0.0)
        if moment - last < self.touch_interval_s:
            return
        self._touched[key_id] = moment
        with self._lock:
            self.conn.execute(*self._update(key_id, [('last_used_at', moment)]))
            self.conn.commit()

    # -- audit ----------------------------------------------------------------

    def _audit(self, key_id, operation, *, object_kind=None, object_id=None, scope=None,
               result='ok', error_code=None):
        payload = _dump(_redact_all(scope)) if scope is not None else None
        row = (self.now(), key_id, operation, object_kind, redact(object_id), payload, result,
               error_code)
        with self._lock:
            self.conn.execute('INSERT INTO audit_log(at, key_id, operation, object_kind, object_id,'
                              ' scope_json, result, error_code) VALUES (?,?,?,?,?,?,?,?)', row)
            if self.audit_retention > 0:
                self.conn.execute('DELETE FROM audit_log WHERE rowid NOT IN'
                                  ' (SELECT rowid FROM audit_log ORDER BY at DESC, rowid DESC'
                                  ' LIMIT ?)', (self.audit_retention,))
            self.conn.commit()

    def read_audit(self, *, actor, key_id=None, operation=None, since=None, limit=200):
        principal = self._as_principal(actor)
        principal.require('admin.audit').raise_if_denied()
        sql = 'SELECT at, key_id, operation, object_kind, object_id, scope_json, result,'
        sql += ' error_code FROM audit_log'
        clauses, values = [], []
        if key_id is not None:
            clauses.append('key_id = ?')
            values.append(key_id)
        if operation is not None:
            clauses.append('operation = ?')
            values.append(operation)
        if since is not None:
            clauses.append('at >= ?')
            values.append(float(since))
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY at DESC, rowid DESC LIMIT ?'
        values.append(max(1, int(limit)))
        with self._lock:
            rows = self.conn.execute(sql, values).fetchall()
        out = []
        for row in rows:
            entry = {name: row[name] for name in AUDIT_LOG_COLUMNS}
            entry['scope'] = _load(entry.pop('scope_json'), None)
            out.append(entry)
        return out

    # -- internals ------------------------------------------------------------

    def _row(self, key_id):
        with self._lock:
            row = self.conn.execute('SELECT * FROM api_keys WHERE id = ?', (key_id,)).fetchone()
        return self._as_record(row)

    def _require_columns(self, *names):
        """Name the missing migration instead of failing inside SQLite.

        The four extra columns are an additive request to ``db.migrate()``; a
        database migrated without them still lists and authenticates keys, and
        says exactly what to run when an operation needs one of them.
        """
        missing = [name for name in names if name not in self._columns]
        if missing:
            raise ApiKeyError(E_INVALID, detail=f'table api_keys is missing: {", ".join(missing)}',
                              action='run apikeys.ensure_schema(conn) or extend migration 8')

    def _insert(self, row):
        """Explicit column list, restricted to the columns this database has.

        The column list is built from module constants, never from user input,
        and dropping a column the database does not have is what lets a key be
        issued on a database that has not taken the additive columns yet.
        """
        present = [(name, value) for name, value in row if name in self._columns]
        columns = ', '.join(name for name, _ in present)
        marks = ', '.join('?' * len(present))
        return (f'INSERT INTO api_keys({columns}) VALUES ({marks})', [value for _, value in present])

    def _update(self, key_id, assignments, *, require=()):
        """An explicit SET list, restricted to the columns this database has."""
        self._require_columns(*require)
        present = [name for name, _ in assignments if name in self._columns]
        if not present:
            raise ApiKeyError(E_INVALID, detail='table api_keys is missing every written column',
                              action='run apikeys.ensure_schema(conn) or extend migration 8')
        values = [value for name, value in assignments if name in self._columns]
        values.append(key_id)
        return (f'UPDATE api_keys SET {", ".join(f"{name} = ?" for name in present)} WHERE id = ?',
                values)

    def _row_by_prefix(self, prefix):
        with self._lock:
            row = self.conn.execute('SELECT * FROM api_keys WHERE prefix = ?',
                                    (prefix,)).fetchone()
        return self._as_record(row)

    def _as_record(self, row):
        """Plain dict with every contract column present.

        A database migrated by ``db.migrate()`` may not have the four columns this
        module adds, and a settings screen must still show the key instead of
        failing on a missing column.
        """
        if row is None:
            return None
        record = {name: row[name] for name in self._columns if name in row.keys()}
        for name in API_KEYS_COLUMNS:
            record.setdefault(name, None)
        return record

    def _as_principal(self, actor):
        if isinstance(actor, Principal):
            return actor
        if isinstance(actor, str):
            return Principal(self.get_key(actor))
        if isinstance(actor, KeyInfo):
            return Principal(actor)
        raise ApiKeyError(E_INVALID, detail='actor must be a key id or an authenticated principal',
                          action='authenticate the caller first')


def _permissions(permissions):
    if permissions is None:
        return set()
    if isinstance(permissions, str):
        permissions = [item.strip() for item in permissions.split(',') if item.strip()]
    if not isinstance(permissions, (list, tuple, set, frozenset)):
        raise ApiKeyError(E_FIELD, detail='permissions must be a list of names',
                          action='pass permissions as a list of documented names')
    granted = set()
    for name in permissions:
        if not isinstance(name, str) or name not in PERMISSIONS:
            raise ApiKeyError(E_UNKNOWN_FIELD, detail=f'unknown permission {name!r}',
                              action='use a permission from the documented set')
        granted.add(name)
    return granted


class Lease:
    """A held slot: concurrency is returned to the key when the holder leaves."""

    __slots__ = ('_guard', '_principal', 'stream_id', 'opened_at', 'last_check', 'closed')

    def __init__(self, guard, principal, stream_id, opened_at):
        self._guard = guard
        self._principal = principal
        self.stream_id = stream_id
        self.opened_at = opened_at
        self.last_check = opened_at
        self.closed = False

    @property
    def key_id(self):
        return self._principal.key_id

    def revalidate(self, now=None):
        """Read the key again. A revoked or expired key loses the lease here."""
        if self.closed:
            raise ApiKeyError(E_INVALID, detail='lease is already closed', action='open a new one')
        info = self._guard.manager.assert_active(self.key_id, operation='lease', now=now)
        self.last_check = self._guard.manager.now() if now is None else float(now)
        self._principal.info = info
        return info

    def close(self):
        if not self.closed:
            self.closed = True
            self._guard._release(self._principal.key_id)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


class QuotaGuard:
    """Rate and concurrency enforcement for one manager.

    The rate window is a sliding window of request timestamps kept in memory: it
    is about protecting the running process, not about accounting across restarts.
    """

    def __init__(self, manager, *, default_window_s=60.0):
        self.manager = manager
        self.default_window_s = float(default_window_s)
        self._lock = threading.RLock()
        self._hits = {}
        self._held = {}

    def check(self, principal, *, now=None, operation='request'):
        """Consume one request from the key's rate budget."""
        limit = principal.rate_limit
        if limit is None:
            return None
        moment = self.manager.now() if now is None else float(now)
        window = limit.window_s or self.default_window_s
        with self._lock:
            hits = self._hits.setdefault(principal.key_id, deque())
            while hits and hits[0] <= moment - window:
                hits.popleft()
            if len(hits) >= limit.requests:
                retry_after = max(0.0, hits[0] + window - moment)
                self.manager._audit(principal.key_id, operation, object_kind='api_key',
                                    object_id=principal.key_id, result='denied',
                                    error_code=E_RATE_LIMITED)
                raise ApiKeyError(E_RATE_LIMITED, detail='rate limit reached',
                                  action='slow down and retry after the window opens',
                                  retry_after=retry_after, state=principal.state)
            hits.append(moment)
        return None

    def acquire(self, principal, *, now=None, stream_id=None, operation='lease'):
        """Take one concurrency slot, after checking the rate budget."""
        self.manager.assert_active(principal.key_id, operation=operation, now=now)
        self.check(principal, now=now, operation=operation)
        moment = self.manager.now() if now is None else float(now)
        limit = principal.concurrency
        with self._lock:
            held = self._held.setdefault(principal.key_id, 0)
            if limit is not None and held >= limit.max_active:
                self.manager._audit(principal.key_id, operation, object_kind='api_key',
                                    object_id=principal.key_id, result='denied',
                                    error_code=E_CONCURRENCY)
                raise ApiKeyError(E_CONCURRENCY, detail='concurrency limit reached',
                                  action='close an open request or stream first',
                                  retry_after=None, state=principal.state)
            self._held[principal.key_id] = held + 1
        return Lease(self, principal, stream_id, moment)

    def active_count(self, key_id=None):
        with self._lock:
            if key_id is None:
                return sum(self._held.values())
            return self._held.get(key_id, 0)

    def forget(self, key_id):
        with self._lock:
            self._hits.pop(key_id, None)
            self._held.pop(key_id, None)

    def _release(self, key_id):
        with self._lock:
            if self._held.get(key_id):
                self._held[key_id] -= 1


class StreamSession:
    """One open event stream. See :class:`StreamSessions` for the policy."""

    __slots__ = ('_sessions', 'stream_id', 'principal', 'opened_at', 'last_check', 'closed',
                 'close_code')

    def __init__(self, sessions, stream_id, principal, opened_at):
        self._sessions = sessions
        self.stream_id = stream_id
        self.principal = principal
        self.opened_at = opened_at
        self.last_check = opened_at
        self.closed = False
        self.close_code = None

    def recheck(self, now=None):
        """Called by the stream before every emitted event, and by the sweeper."""
        if self.closed:
            raise ApiKeyError(E_INVALID, detail='stream session is closed',
                              action='open a new stream')
        info = self._sessions.manager.assert_active(self.principal.key_id, operation='event.stream',
                                                     now=now)
        self.last_check = self._sessions.manager.now() if now is None else float(now)
        self.principal.info = info
        return info

    def close(self, code=None):
        if self.closed:
            return
        self.closed = True
        self.close_code = code
        self._sessions._drop(self.stream_id)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


class StreamSessions:
    """Registry of open SSE/stream sessions with a written expiry policy.

    Policy (CONTRACTS §5.2): a new subscription is checked at once against
    ``admin``/key state; an open session re-reads the key at least every
    ``recheck_interval_s`` and again before each emitted event; a revoke or an
    expiry closes the session with ``E_AUTH_REVOKED`` or ``E_AUTH_EXPIRED`` and
    no further events are written.  ``sweep()`` is what makes that prompt — the
    API layer calls it on its own timer, and an open session finds out on its own
    next recheck even if nobody sweeps.
    """

    def __init__(self, manager, *, recheck_interval_s=STREAM_RECHECK_INTERVAL_S):
        self.manager = manager
        self.recheck_interval_s = float(recheck_interval_s)
        self._lock = threading.RLock()
        self._sessions = {}

    def open(self, principal, stream_id, *, now=None):
        info = self.manager.assert_active(principal.key_id, operation='event.stream.subscribe',
                                          now=now)
        principal.info = info
        moment = self.manager.now() if now is None else float(now)
        session = StreamSession(self, stream_id, principal, moment)
        with self._lock:
            self._sessions[stream_id] = session
        return session

    def due(self, now=None):
        """Sessions whose recheck interval has passed."""
        moment = self.manager.now() if now is None else float(now)
        with self._lock:
            return [session for session in self._sessions.values()
                    if moment - session.last_check >= self.recheck_interval_s]

    def sweep(self, now=None):
        """Close every session whose key is no longer usable. Returns the terminations."""
        terminated = []
        for session in self.active():
            if session.closed:
                continue
            try:
                session.recheck(now)
            except ApiKeyError as error:
                session.close(error.code)
                terminated.append((session.stream_id, error.code))
        return terminated

    def active(self):
        with self._lock:
            return list(self._sessions.values())

    def active_ids(self):
        with self._lock:
            return list(self._sessions)

    def close(self, stream_id, code=None):
        with self._lock:
            session = self._sessions.get(stream_id)
        if session is not None:
            session.close(code)

    def _drop(self, stream_id):
        with self._lock:
            self._sessions.pop(stream_id, None)
