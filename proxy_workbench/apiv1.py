"""Versioned public control API ``/v1``.

This module owns the HTTP contract of the control API and nothing else: the
versioned prefix, the route table, authentication, permissions, resource
scopes, validation, limits, idempotency, cursors, the event stream and the
OpenAPI artifact.  It owns no database, no worker and no second copy of the
domain logic.  Both dependencies are injected:

* ``keys``    -- a key store (see :class:`KeyStore`), implemented by
  ``proxy_workbench.apikeys``; the secret is hashed there and never kept here.
* ``service`` -- the shared service layer used by CLI and GUI as well (F18),
  reached through :class:`Service`; one ``invoke`` call is one operation.

Integration contract: CONTRACTS.ru.md 5 (version 1).
Requirements: F29 (API and key manager surface), F18 (one management layer),
F07 (no silently ignored parameter), R03, R12, R17, R18.

One complete flow, as a client sees it::

    POST /v1/keys                       -> create a scoped key, secret shown once
    POST /v1/collections                -> {"name": "mine", "kind": "own"}
    POST /v1/collections/c1/imports     -> 202, job id, one collection only
    POST /v1/checks/check               -> 202, job id, scope fixed at submit
    GET  /v1/jobs/{job}/events          -> text/event-stream, resume with the last id:
    GET  /v1/results?sort=quality       -> fresh results, opaque cursor
    POST /v1/exports                    -> 202, artifact of the selected scope
    GET  /v1/exports/{id}/download/name -> bytes of that artifact
    POST /v1/keys/{id}/revoke           -> the key stops working at once

Every mutation carries ``Idempotency-Key``; every change of a revisioned object
carries ``If-Match``; every answer is a JSON object or a documented ``E_*`` error
with a recovery action.  The packaged OpenAPI artifact is
:func:`openapi_path`, generated from :func:`openapi_document`.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import ssl
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qs, urlsplit

from .branding import PRODUCT_NAME, PRODUCT_VERSION
from .i18n import tr

API_PREFIX = '/v1'
API_VERSION = '1.0.0'
OPENAPI_FILE = 'openapi.json'
DEFAULT_PORT = 8766
LOOPBACK_HOSTS = ('localhost', '127.0.0.1', '::1')
MAX_PAGE_LIMIT = 1000
MAX_BODY_BYTES = 8 * 1024 * 1024

# --- permissions: the canon of CONTRACTS.ru.md 5.2.  Nothing is allowed by default.
READ_PERMISSIONS = frozenset({
    'read.status', 'read.results', 'read.results.detail', 'read.export.artifact',
})
PERMISSIONS = frozenset({
    'read.status', 'read.results', 'read.results.detail', 'read.export.artifact',
    'collections.read', 'collections.write', 'import.read', 'import.commit',
    'profiles.read', 'profiles.write',
    'sources.read', 'sources.write',
    'jobs.read', 'jobs.submit', 'jobs.control',
    'pools.read', 'pools.write',
    'gateway.read', 'gateway.write',
    'schedules.read', 'schedules.write',
    'export.create', 'export.secret',
    'admin.settings', 'admin.keys', 'admin.audit',
})
# A subscription secret serves clients that cannot send a header.  It is narrow
# on purpose: no admin, no writes, no private fields.
SUBSCRIPTION_PERMISSIONS = frozenset({'read.status', 'read.results', 'read.export.artifact'})
# The legacy read token keeps exactly what /proxies, /random, /status, /pac,
# /clash and /singbox gave it.  No admin and no private scope is added.
LEGACY_READ_PERMISSIONS = frozenset({'read.status', 'read.results', 'read.export.artifact'})
# Stripped from every response body for a subscription or legacy identity.
REDACTED_FIELDS = frozenset({
    'secret', 'password', 'credentials', 'upstream_credential', 'access_secret',
    'gateway_password', 'verifier', 'verifier_salt', 'authorization',
})

# --- error codes: CONTRACTS.ru.md 5.4, plus the three this surface adds.
ERROR_DOMAIN_STATUS = {
    'AUTH': 401, 'VALIDATION': 400, 'CONFLICT': 409, 'STATE': 409, 'TIME': 409,
    'DATA': 409, 'IMPORT': 400, 'SECRET': 409, 'LIMIT': 413, 'GATEWAY': 502,
}
CODE_STATUS = {
    'E_VALIDATION_METHOD': 405,
    'E_STATE_NOT_FOUND': 404,
    'E_AUTH_ORIGIN': 403,
    'E_LIMIT_QUEUE': 429,
    'E_LIMIT_CONCURRENCY': 429,
    'E_SERVICE_UNAVAILABLE': 503,
}
MESSAGES = {
    'E_AUTH_MISSING': (
        'Требуется ключ: передайте его в заголовке Authorization: Bearer.',
        'a key is required: send it in the Authorization: Bearer header'),
    'E_AUTH_INVALID': ('Ключ не найден или неверный.', 'the key is unknown or wrong'),
    'E_AUTH_EXPIRED': ('Срок действия ключа истёк.', 'the key has expired'),
    'E_AUTH_REVOKED': ('Ключ отозван.', 'the key has been revoked'),
    'E_AUTH_DISABLED': ('Ключ отключён администратором.', 'the key is disabled by an administrator'),
    'E_AUTH_PERMISSION': ('У ключа нет права на эту операцию.',
                           'the key has no permission for this operation'),
    'E_AUTH_SCOPE': ('Ключ не ограничен этим объектом.', 'the key is not scoped to this object'),
    'E_AUTH_RATE_LIMITED': ('Превышен лимит запросов для этого ключа.',
                            'the request rate limit for this key is exceeded'),
    'E_AUTH_ORIGIN': ('Заголовок Host или Origin не соответствует сетевой модели.',
                      'the Host or Origin header does not match the network model'),
    'E_AUTH_ROTATION_GRACE': ('Ключ вне окна ротации.', 'the key is outside its rotation grace window'),
    'E_VALIDATION_SCHEMA': ('Тело запроса должно быть объектом JSON.',
                            'the request body must be a JSON object'),
    'E_VALIDATION_FIELD': ('Поле не прошло проверку.', 'a field did not pass validation'),
    'E_VALIDATION_UNKNOWN_FIELD': ('Неизвестное поле: молча игнорировать параметр нельзя.',
                                   'unknown field: a parameter is never ignored silently'),
    'E_VALIDATION_METHOD': ('Метод не поддерживается для этого пути.',
                            'this method is not supported for this path'),
    'E_CONFLICT_REVISION': ('Ревзия устарела: перечитайте объект и повторите.',
                            'the revision is stale: read the object again and retry'),
    'E_CONFLICT_IDEMPOTENCY': ('Idempotency-Key уже использован с другим телом.',
                               'the Idempotency-Key was already used with a different body'),
    'E_CONFLICT_BUSY': ('База занята другим исполнителем.', 'another writer holds the database'),
    'E_STATE_NOT_FOUND': ('Объект не найден или недоступен для этого ключа.',
                          'the object does not exist or is not available to this key'),
    'E_STATE_NO_SNAPSHOT': ('Нет опубликованного снимка.', 'no published snapshot exists'),
    'E_STATE_SNAPSHOT_STALE': ('Снимок истёк.', 'the snapshot has expired'),
    'E_STATE_JOB_RUNNING': ('Задание уже выполняется.', 'the job is already running'),
    'E_TIME_UNKNOWN': ('Время измерения неизвестно.', 'the measurement time is unknown'),
    'E_DATA_DB_VERSION_AHEAD': ('База создана более новой версией программы.',
                                'the database was created by a newer program version'),
    'E_SECRET_UPSTREAM_AUTH_REQUIRED': ('Для этого endpoint нужны учётные данные.',
                                        'this endpoint needs upstream credentials'),
    'E_LIMIT_BODY': ('Тело запроса больше допустимого.', 'the request body is too large'),
    'E_LIMIT_QUEUE': ('Очередь заполнена.', 'the queue is full'),
    'E_LIMIT_CONCURRENCY': ('Достигнут лимит одновременных операций.',
                            'the concurrency limit is reached'),
    'E_LIMIT_BUDGET': ('Бюджет задания исчерпан.', 'the job budget is exhausted'),
    'E_SERVICE_UNAVAILABLE': ('Сервисный слой не отвечает или вернул неверный ответ.',
                              'the service layer is unavailable or returned an invalid answer'),
}
STATUS_ERROR_CODE = {
    400: 'E_VALIDATION_FIELD', 401: 'E_AUTH_MISSING', 403: 'E_AUTH_PERMISSION',
    404: 'E_STATE_NOT_FOUND', 405: 'E_VALIDATION_METHOD', 409: 'E_CONFLICT_REVISION',
    410: 'E_STATE_SNAPSHOT_STALE', 413: 'E_LIMIT_BODY', 429: 'E_LIMIT_QUEUE',
    500: 'E_SERVICE_UNAVAILABLE', 502: 'E_GATEWAY_NO_UPSTREAM', 504: 'E_GATEWAY_DEADLINE',
}

ID_PATTERN = re.compile(r'^[A-Za-z0-9._:-]{1,128}$')
FILE_CHARS = re.compile(r'[^A-Za-z0-9._-]')


def default_status(code):
    """HTTP status of an ``E_*`` code; the code decides it, not the call site."""
    if code in CODE_STATUS:
        return CODE_STATUS[code]
    domain = code.split('_', 2)[1] if code.count('_') >= 2 else 'STATE'
    return ERROR_DOMAIN_STATUS.get(domain, 400)


class ApiError(Exception):
    """One documented error: stable code, localizable text, a recovery action."""

    def __init__(self, code, *, action=None, details=None, status=None,
                 retry_after=None, message=None, allow=None):
        super().__init__(code)
        self.code = code
        self.status = status or default_status(code)
        self.message = message or tr(*MESSAGES.get(code, (code, code)))
        self.action = action
        self.details = details or {}
        self.retry_after = retry_after
        self.allow = allow

    def body(self):
        error = {'code': self.code, 'message': self.message}
        if self.action:
            error['action'] = self.action
        if self.details:
            error['details'] = self.details
        return {'error': error}


def field_error(name, detail, *, code='E_VALIDATION_FIELD', action=None):
    return ApiError(code, action=action, details={'field': name, 'reason': detail})


#: A domain refusal the service layer already decided on, by the domain of its code.
#: The catalogue has no entry for a code it does not know, so the exception's own
#: message is used; the code itself stays the one the layer produced.
DOMAIN_STATUS = (
    ('E_AUTH_', 403), ('E_VALIDATION_', 400), ('E_CONFLICT_', 409),
    ('E_SECRET_', 409), ('E_LIMIT_', 429), ('E_TIME_', 409),
    ('E_STATE_', 409), ('E_EXPORT_', 409), ('E_DATA_', 409), ('E_GATEWAY_', 502),
)


def _domain_refusal(exc):
    """Translate a domain error of the service layer into the API's own answer.

    Every exception used to become ``E_SERVICE_UNAVAILABLE`` -- "the operation is
    not wired to the service layer" -- so a refusal the engine had already decided
    on (no snapshot, credentials not granted, nothing to export) reached the
    caller as a broken feature.  A refusal is a refusal, not an outage (F29,
    F24: a defect must not be reported as a missing function).
    """
    code = getattr(exc, 'code', None)
    if not isinstance(code, str) or not code.startswith('E_'):
        return None
    status = next((value for prefix, value in DOMAIN_STATUS if code.startswith(prefix)), 409)
    message = getattr(exc, 'message', None) or str(exc)
    if code in MESSAGES:
        return ApiError(code, status=status)
    return ApiError(code, status=status, message=tr(message, message),
                    details={'reason': type(exc).__name__})


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Field:
    """One validated parameter.  Unknown parameters are rejected, never ignored."""
    name: str
    kind: str = 'string'          # string, int, float, bool, list, object
    required: bool = False
    default: Any = None
    choices: tuple = ()
    minimum: float | None = None
    maximum: float | None = None
    max_len: int = 4096
    max_items: int = 512
    min_items: int = 0
    shape: tuple = ()              # nested fields of an object

    def spec(self):
        json_type = {'string': 'string', 'int': 'integer', 'float': 'number',
                     'bool': 'boolean', 'list': 'array', 'object': 'object'}[self.kind]
        out = {'type': json_type}
        if self.required:
            out['description'] = 'required'
        if self.choices:
            out['enum'] = list(self.choices)
        if self.minimum is not None:
            out['minimum'] = self.minimum
        if self.maximum is not None:
            out['maximum'] = self.maximum
        if self.kind == 'string':
            out['maxLength'] = self.max_len
        if self.kind == 'list':
            out['maxItems'] = self.max_items
            if self.min_items:
                out['minItems'] = self.min_items
        if self.shape:
            out['properties'] = {f.name: f.spec() for f in self.shape}
        return out


TRUE_VALUES = ('1', 'true', 'yes', 'on')
FALSE_VALUES = ('0', 'false', 'no', 'off')


def _validate_scalar(field, value):
    if field.kind == 'string':
        if not isinstance(value, str):
            raise field_error(field.name, tr('ожидается строка', 'a string is expected'))
        if len(value) > field.max_len:
            raise field_error(field.name, tr(f'длиннее {field.max_len} символов',
                                              f'longer than {field.max_len} characters'))
        if field.choices and value not in field.choices:
            raise field_error(field.name, ', '.join(field.choices))
        return value
    if field.kind in ('int', 'float'):
        if isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                raise field_error(field.name, tr('ожидается число', 'a number is expected')) from None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise field_error(field.name, tr('ожидается число', 'a number is expected'))
        if field.kind == 'int':
            if float(value) != int(value):
                raise field_error(field.name, tr('ожидается целое', 'an integer is expected'))
            value = int(value)
        if field.minimum is not None and value < field.minimum:
            raise field_error(field.name, f'>= {field.minimum:g}')
        if field.maximum is not None and value > field.maximum:
            raise field_error(field.name, f'<= {field.maximum:g}')
        return value
    if field.kind == 'bool':
        if isinstance(value, str):
            if value.lower() in TRUE_VALUES:
                return True
            if value.lower() in FALSE_VALUES:
                return False
        elif isinstance(value, bool):
            return value
        raise field_error(field.name, 'true или false')
    if field.kind == 'list':
        if not isinstance(value, list):
            raise field_error(field.name, tr('ожидается список', 'a list is expected'))
        if len(value) > field.max_items:
            raise field_error(field.name, f'<= {field.max_items} items')
        if len(value) < field.min_items:
            raise field_error(field.name, tr(f'нужно не меньше {field.min_items} элементов',
                                              f'at least {field.min_items} items are required'),
                              action=tr('пустой набор целей не даёт pass',
                                        'an empty set of targets cannot pass'))
        return value
    if field.kind == 'object':
        if not isinstance(value, dict):
            raise field_error(field.name, tr('ожидается объект', 'an object is expected'))
        return validate_object(field.shape, value, field.name)
    raise field_error(field.name, 'unsupported field type')


def validate_object(fields, data, where='body'):
    """Validate a JSON object against a closed set of fields."""
    if not isinstance(data, dict):
        raise ApiError('E_VALIDATION_SCHEMA', details={'where': where})
    known = {f.name: f for f in fields}
    unknown = sorted(set(data) - set(known))
    if unknown:
        raise ApiError('E_VALIDATION_UNKNOWN_FIELD',
                       action=tr('уберите поле или используйте документированное',
                                 'remove the field or use a documented one'),
                       details={'where': where, 'fields': unknown})
    out = {}
    for f in fields:
        if f.name in data:
            out[f.name] = _validate_scalar(f, data[f.name])
        elif f.required:
            raise field_error(f.name, tr('обязательное поле', 'required field'),
                              action=tr('добавьте поле', 'add the field'))
        elif f.default is not None:
            out[f.name] = f.default
    return out


def parse_query_fields(fields, query, *, extra=()):
    """Validate a query string against a closed set of parameters."""
    values = {key: items[-1] for key, items in parse_qs(query, keep_blank_values=True).items()
              if items}
    known = {f.name: f for f in fields}
    known.update({name: Field(name, 'string', max_len=4096) for name in extra})
    unknown = sorted(set(values) - set(known))
    if unknown:
        raise ApiError('E_VALIDATION_UNKNOWN_FIELD',
                       action=tr('уберите параметр или используйте документированный',
                                 'remove the parameter or use a documented one'),
                       details={'where': 'query', 'fields': unknown})
    return {name: _validate_scalar(known[name], text) for name, text in values.items()}


# --------------------------------------------------------------------------
# identities
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Principal:
    """Who is calling.  Four identities exist and none substitutes for another."""
    key_id: str
    kind: str = 'api_key'          # api_key | subscription | legacy | bootstrap
    permissions: frozenset = frozenset()
    collections: tuple = ()        # empty = everything the permissions allow
    pools: tuple = ()
    name: str | None = None
    purpose: str | None = None
    expires_at: float | None = None
    revoked_at: float | None = None
    rotation_grace_until: float | None = None
    rate_limit: tuple | None = None        # (requests, window_seconds)
    concurrency: int | None = None
    last_used_at: float | None = None

    def has(self, permission):
        return permission in self.permissions

    def allows(self, kind, object_id):
        """True when the resource scope of the key covers the object."""
        if object_id is None or kind not in ('pool', 'collection'):
            return True
        allowed = self.pools if kind == 'pool' else self.collections
        return not allowed or object_id in allowed

    @property
    def redacted(self):
        return self.kind in ('subscription', 'legacy')

    def check_time(self, now):
        if self.revoked_at is not None and self.revoked_at <= now:
            raise ApiError('E_AUTH_REVOKED', status=401)
        if self.expires_at is not None and self.expires_at <= now:
            raise ApiError('E_AUTH_EXPIRED', status=401)


def require_permission(principal, permission, now):
    """Nothing is allowed by default; a missing key is a different error than a weak one."""
    if principal is None:
        raise ApiError('E_AUTH_MISSING', action=tr(
            'передайте ключ в заголовке Authorization: Bearer',
            'send the key in the Authorization: Bearer header'))
    principal.check_time(now)
    if not principal.has(permission):
        raise ApiError('E_AUTH_PERMISSION', status=403,
                       details={'required': permission, 'granted': sorted(principal.permissions)},
                       action=tr('создайте ключ с этим правом', 'create a key with this permission'))


def require_scope(principal, kind, object_id):
    """A key must not even learn whether an out-of-scope object exists."""
    if object_id is None or principal is None or principal.allows(kind, object_id):
        return
    raise ApiError('E_STATE_NOT_FOUND', status=404,
                   action=tr('объект вне scope ключа', 'the object is outside the key scope'))


class KeyStore:
    """Key storage implemented by ``proxy_workbench.apikeys``.

    ``verify`` receives a presented secret and returns a :class:`Principal` or
    ``None``.  It is a pure function: no logging and no storage of the plaintext.
    """

    REQUIRED = ('verify', 'list_keys', 'get_key', 'create_key', 'update_key', 'rotate_key',
                'revoke_key', 'disable_key', 'enable_key', 'delete_key', 'read_audit', 'audit')

    def verify(self, secret):  # pragma: no cover - interface
        raise NotImplementedError

    def list_keys(self, principal, limit=None, **kwargs):  # pragma: no cover - interface
        raise NotImplementedError

    def get_key(self, principal, key_id):  # pragma: no cover - interface
        raise NotImplementedError

    def create_key(self, principal, spec):  # pragma: no cover - interface
        raise NotImplementedError

    def update_key(self, principal, key_id, patch):  # pragma: no cover - interface
        raise NotImplementedError

    def rotate_key(self, principal, key_id, grace_s=0.0, **kwargs):  # pragma: no cover - interface
        raise NotImplementedError

    def revoke_key(self, principal, key_id, **kwargs):  # pragma: no cover - interface
        raise NotImplementedError

    def disable_key(self, principal, key_id, **kwargs):  # pragma: no cover - interface
        raise NotImplementedError

    def enable_key(self, principal, key_id, **kwargs):  # pragma: no cover - interface
        raise NotImplementedError

    def delete_key(self, principal, key_id, **kwargs):  # pragma: no cover - interface
        raise NotImplementedError

    def read_audit(self, principal, **filters):  # pragma: no cover - interface
        raise NotImplementedError

    def audit(self, record):  # pragma: no cover - interface
        raise NotImplementedError


class Service:
    """The shared service layer (F18).  One ``invoke`` per API operation."""

    REQUIRED = ('invoke', 'queue_state')

    def invoke(self, operation, call):  # pragma: no cover - interface
        raise NotImplementedError

    def queue_state(self):  # pragma: no cover - interface
        raise NotImplementedError


def require_object(obj, required, label):
    missing = [name for name in required if not callable(getattr(obj, name, None))]
    if missing:
        raise TypeError(f'{label} is missing: {", ".join(missing)}')


class ApiKeyStore:
    """The :class:`KeyStore` of this module, bound to ``apikeys.ApiKeyManager``.

    The manager keeps the one-way verifiers, the permissions, the resource scope
    and the audit rows; this adapter only translates its vocabulary into the one
    the request pipeline speaks.  Nothing here re-implements a key check.
    """

    REQUIRED = KeyStore.REQUIRED

    def __init__(self, manager):
        self.manager = manager

    # -- translation ---------------------------------------------------------

    @staticmethod
    def _translate(exc, not_found=False):
        """An apikeys error keeps its code; an unknown key id is a 404 here."""
        original = getattr(exc, 'code', 'E_AUTH_INVALID')
        body = exc.as_json() if hasattr(exc, 'as_json') else {}
        code = 'E_STATE_NOT_FOUND' if original == 'E_VALIDATION_FIELD' and not_found else original
        known = code if code in MESSAGES else 'E_AUTH_INVALID'
        details = dict(body.get('details') or {})
        if body.get('detail'):
            details['reason'] = body['detail']
        # a remapped code owns its status; an unchanged one keeps the manager's
        status = getattr(exc, 'http_status', None) if known == original else None
        return ApiError(known, action=body.get('action'), details=details or None,
                        status=status, retry_after=body.get('retry_after'))

    def _call(self, operation, not_found=False, **kwargs):
        try:
            return getattr(self.manager, operation)(**kwargs)
        except Exception as exc:
            if type(exc).__name__ != 'ApiKeyError':
                raise
            raise self._translate(exc, not_found) from None

    def _principal_of(self, principal):
        """A key that holds nothing but the read rights is a narrow subscription."""
        permissions = frozenset(principal.info.permissions)
        kind = 'api_key'
        if permissions and permissions <= SUBSCRIPTION_PERMISSIONS and \
                not permissions & (PERMISSIONS - SUBSCRIPTION_PERMISSIONS):
            kind = 'subscription'
        scope = principal.scope
        rate = principal.rate_limit
        return Principal(
            key_id=principal.key_id, kind=kind, permissions=permissions,
            collections=tuple(scope.collections), pools=tuple(scope.pools),
            name=principal.info.name, expires_at=principal.info.expires_at,
            revoked_at=principal.info.revoked_at or principal.info.disabled_at,
            rotation_grace_until=principal.info.rotation_grace_until,
            rate_limit=(rate.requests, rate.window_s) if rate else None,
            concurrency=principal.concurrency.max_active if principal.concurrency else None,
            last_used_at=principal.info.last_used_at)

    @staticmethod
    def _scope_of(spec):
        scope = {}
        if spec.get('collections'):
            scope['collections'] = list(spec['collections'])
        if spec.get('pools'):
            scope['pools'] = list(spec['pools'])
        return scope or None

    # -- KeyStore ------------------------------------------------------------

    def verify(self, secret):
        if not secret:
            return None
        return self._principal_of(self._call('authenticate', secret=secret))

    def list_keys(self, principal, limit=None, **kwargs):
        infos = self._call('list_keys', actor=principal.key_id, limit=limit or 500)
        now = self.manager.now()
        return {'items': [info.as_dict(now) for info in infos], 'stream_id': 'keys',
                'next_seq': None}

    def create_key(self, principal, spec):
        rate = None
        if spec.get('rate_limit_requests'):
            rate = (spec['rate_limit_requests'], spec['rate_limit_window_seconds'])
        issued = self._call('create', actor=principal.key_id, name=spec['name'],
                            purpose=spec.get('purpose'), permissions=spec['permissions'],
                            scope=self._scope_of(spec), expires_at=spec.get('expires_at'),
                            rate_limit=rate, concurrency=spec.get('concurrency'))
        return issued.as_json()

    def rotate_key(self, principal, key_id, grace_s=0.0, **kwargs):
        issued = self._call('rotate', not_found=True, key_id=key_id, actor=principal.key_id,
                            grace_s=float(grace_s or 0.0))
        return issued.as_json()

    def revoke_key(self, principal, key_id, **kwargs):
        info = self._call('revoke', not_found=True, key_id=key_id, actor=principal.key_id)
        return info.as_dict(self.manager.now())

    def disable_key(self, principal, key_id, **kwargs):
        info = self._call('disable', not_found=True, key_id=key_id, actor=principal.key_id)
        return info.as_dict(self.manager.now()) if info is not None else {'id': key_id}

    def enable_key(self, principal, key_id, **kwargs):
        info = self._call('enable', not_found=True, key_id=key_id, actor=principal.key_id)
        return info.as_dict(self.manager.now()) if info is not None else {'id': key_id}

    def delete_key(self, principal, key_id, **kwargs):
        self._call('delete', not_found=True, key_id=key_id, actor=principal.key_id)
        return {'id': key_id, 'deleted': True}

    def update_key(self, principal, key_id, patch):
        fields = {name: patch[name] for name in ('name', 'purpose', 'expires_at') if name in patch}
        info = self._call('update_metadata', not_found=True, key_id=key_id,
                          actor=principal.key_id, **fields)
        return info.as_dict(self.manager.now())

    def get_key(self, principal, key_id):
        info = self._call('get_key', not_found=True, key_id=key_id, actor=principal.key_id)
        return info.as_dict(self.manager.now())

    def read_audit(self, principal, **filters):
        rows = self._call('read_audit', actor=principal.key_id,
                          limit=int(filters.get('limit') or 200))
        return {'items': rows, 'stream_id': 'audit', 'next_seq': None}

    def audit(self, record):
        """One audit row per answered mutation; never a body, never a secret."""
        record = getattr(self.manager, 'record_audit', None) or self.manager._audit
        record(record['key_id'], record['operation'],
               object_kind=record.get('object_kind'), object_id=record.get('object_id'),
               scope={k: v for k, v in record.items()
                      if k in ('collection_id', 'pool_id') and v},
               result=record.get('result') or 'ok', error_code=record.get('error_code'))


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ApiConfig:
    """Network model and bounds.  Loopback is the default and stays the default."""
    host: str = '127.0.0.1'
    port: int = DEFAULT_PORT
    max_body_bytes: int = MAX_BODY_BYTES
    max_limit: int = MAX_PAGE_LIMIT
    max_queue_depth: int = 100
    max_concurrency: int = 16
    max_event_history: int = 1000
    request_timeout_s: float = 60.0               # the deadline handed to the service
    rate_limit: tuple = (240, 60)                  # requests, window seconds
    idempotency_ttl_s: float = 900.0
    idempotency_max_entries: int = 512
    require_idempotency_key: bool = True
    allow_query_token: bool = True                # documented legacy path
    allowed_hosts: tuple = ()                     # empty = loopback Host only
    allowed_origins: tuple = ()                   # Origin accepted from a browser
    cors_origins: tuple = ()                      # subset echoed back in CORS headers
    allowed_bind_hosts: tuple = ()                # non-loopback gateway bind addresses
    allow_remote_bind: bool = False
    tls_cert: str | None = None
    tls_key: str | None = None
    sse_reverify_s: float = 15.0

    @property
    def loopback_only(self):
        return not self.allow_remote_bind


def is_loopback_host(host):
    """True for localhost, 127.0.0.1 and ::1, with or without a port."""
    name = (host or '').strip()
    if name.startswith('['):
        name = name[1:name.index(']')] if ']' in name else name[1:]
    elif name.count(':') == 1:
        name = name.rsplit(':', 1)[0]
    return name.lower() in LOOPBACK_HOSTS


# --------------------------------------------------------------------------
# bounded runtime state
# --------------------------------------------------------------------------

class RateLimiter:
    """Sliding window per identity and path.  Excess gives 429 and a Retry-After."""

    def __init__(self, clock):
        self._clock = clock
        self._hits = {}
        self._lock = threading.Lock()

    def check(self, bucket, limit, window):
        now = self._clock()
        with self._lock:
            hits = deque(self._hits.get(bucket, ()))
            while hits and hits[0] <= now - window:
                hits.popleft()
            if len(hits) >= limit:
                retry = max(1, int(math.ceil(hits[0] + window - now)))
                self._hits[bucket] = hits
                raise ApiError('E_AUTH_RATE_LIMITED', status=429, retry_after=retry,
                               details={'limit': limit, 'window_seconds': window},
                               action=tr('повторите позже', 'retry later'))
            hits.append(now)
            self._hits[bucket] = hits
            if len(self._hits) > 4096:
                self._hits.pop(next(iter(self._hits)))


class ConcurrencyLimiter:
    """Bounded number of in-flight operations; a slot is always released."""

    def __init__(self, maximum):
        self.maximum = maximum
        self._used = 0
        self._lock = threading.Lock()

    def acquire(self):
        with self._lock:
            if self._used >= self.maximum:
                raise ApiError('E_LIMIT_CONCURRENCY', status=429, retry_after=1,
                               details={'maximum': self.maximum},
                               action=tr('повторите после освобождения слота',
                                         'retry once a slot is free'))
            self._used += 1

    def release(self):
        with self._lock:
            self._used = max(0, self._used - 1)


class IdempotencyStore:
    """Bounded, expiring record of answered mutations.  One key, one answer."""

    def __init__(self, ttl_s, maximum, clock):
        self.ttl_s = ttl_s
        self.maximum = maximum
        self._clock = clock
        self._records = OrderedDict()
        self._lock = threading.Lock()

    def get(self, bucket, key, digest):
        with self._lock:
            record = self._records.get((bucket, key))
            if record is None:
                return None
            if record['digest'] != digest:
                raise ApiError('E_CONFLICT_IDEMPOTENCY', status=409,
                               details={'idempotency_key': key},
                               action=tr('используйте новый ключ или тот же запрос',
                                         'use a new key or repeat the same request'))
            if self._clock() - record['at'] > self.ttl_s:
                self._records.pop((bucket, key), None)
                return None
            return record['response']

    def put(self, bucket, key, digest, response):
        with self._lock:
            self._records[(bucket, key)] = {'digest': digest, 'response': response,
                                            'at': self._clock()}
            self._records.move_to_end((bucket, key))
            while len(self._records) > self.maximum:
                self._records.popitem(last=False)


class EventHistory:
    """Bounded history of emitted cursors, per stream.  A resume past it is refused."""

    def __init__(self, maximum):
        self.maximum = maximum
        self._streams = OrderedDict()
        self._total = 0
        self._lock = threading.Lock()

    def record(self, stream_id, seq):
        with self._lock:
            stream = self._streams.get(stream_id)
            if stream is None:
                stream = deque()
                self._streams[stream_id] = stream
            stream.append(seq)
            self._total += 1
            while self._total > self.maximum and stream:
                stream.popleft()
                self._total -= 1
            self._streams.move_to_end(stream_id)
            while len(self._streams) > 64:
                _, dropped = self._streams.popitem(last=False)
                self._total -= len(dropped)

    def known(self, stream_id, seq):
        """None when unknown, True when retained, False when the history evicted it."""
        with self._lock:
            stream = self._streams.get(stream_id)
            if stream is None:
                return None
            return bool(stream) and seq in stream


# --------------------------------------------------------------------------
# cursors: one scheme for pagination and for events (CONTRACTS.ru.md 5.7)
# --------------------------------------------------------------------------

def digest_of(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     default=str).encode('utf-8')).hexdigest()


def encode_cursor(stream_id, seq, digest=None):
    raw = json.dumps({'s': stream_id, 'n': int(seq), 'd': (digest or '')[:16]},
                     sort_keys=True, separators=(',', ':')).encode('utf-8')
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def decode_cursor(cursor):
    try:
        padded = cursor + '=' * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode('ascii')).decode('utf-8'))
        return str(data['s']), int(data['n']), str(data.get('d') or '')
    except (ValueError, TypeError, KeyError) as exc:
        raise field_error('cursor', tr('нечитаемый курсор', 'unreadable cursor'),
                          action=tr('начните выдачу заново', 'restart the listing')) from exc


# --------------------------------------------------------------------------
# requests, responses, event stream
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Request:
    method: str
    path: str
    query: str = ''
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b''
    client_host: str = '127.0.0.1'

    def header(self, name, default=None):
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return default


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes = b''
    content_type: str = 'application/json; charset=utf-8'
    headers: tuple = ()
    stream: Any = None

    @property
    def status_code(self):
        return self.status

    def json(self):
        return json.loads(self.body.decode('utf-8'))

    @property
    def text(self):
        return self.body.decode('utf-8', 'replace')


@dataclass(frozen=True)
class Call:
    """What the service layer receives for one operation."""
    operation: str
    principal: Principal
    params: Mapping[str, str] = field(default_factory=dict)
    query: Mapping[str, Any] = field(default_factory=dict)
    body: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None
    expected_revision: int | None = None
    deadline_s: float | None = None
    path: str = ''
    method: str = 'GET'


class EventStream:
    """Server-sent events of one stream, normalized to the documented frame."""

    def __init__(self, stream_id, events, clock, history=None, cursor_seq=0):
        self.stream_id = stream_id
        self.events = events
        self.clock = clock
        self.history = history
        self.cursor_seq = cursor_seq

    def __iter__(self):
        last = self.cursor_seq
        for raw in self.events:
            if not isinstance(raw, dict) or 'seq' not in raw or 'type' not in raw:
                yield self._frame({'seq': last + 1, 'type': 'error',
                                   'code': 'E_VALIDATION_FIELD',
                                   'data': {'reason': 'event needs seq and type'}})
                return
            seq = int(raw['seq'])
            if seq <= last:
                yield self._frame({'seq': last + 1, 'type': 'error',
                                   'code': 'E_CONFLICT_REVISION',
                                   'data': {'reason': 'seq must increase', 'seq': seq,
                                            'last_seq': last}})
                return
            if self.history is not None:
                self.history.record(self.stream_id, seq)
            last = seq
            yield self._frame(raw)

    def _frame(self, raw):
        event = {'stream': self.stream_id, 'seq': int(raw.get('seq') or 0),
                 'at': float(raw.get('at') or self.clock()),
                 'type': str(raw.get('type') or 'unknown'),
                 'job_id': raw.get('job_id'), 'item_id': raw.get('item_id'),
                 'code': raw.get('code'), 'data': raw.get('data') or {}}
        payload = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
        return (f'id: {encode_cursor(self.stream_id, event["seq"])}\n'
                f'event: {event["type"]}\n'
                f'data: {payload}\n\n')


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Route:
    method: str
    path: str
    operation: str
    permission: str | None
    summary: str
    mutating: bool = False
    async_job: bool = False
    paginated: bool = False
    revisioned: bool = False
    sse: bool = False
    raw_body: bool = False
    anonymous: bool = False
    legacy_token: bool = False
    scope: tuple = ()             # ((kind, path param or body field),) checked before the call
    sensitive: tuple = ()         # ((field, extra permission required when the field is true),)
    guard: str = ''               # named check that keeps one operation inside its bounds
    body: tuple = ()
    query: tuple = ()
    tags: tuple = ()

    def spec(self):
        parameters = [{'name': part[1:-1], 'in': 'path', 'required': True,
                       'schema': {'type': 'string'}}
                      for part in self.path[len(API_PREFIX):].strip('/').split('/')
                      if part.startswith('{')]
        if self.query:
            parameters.append({'name': 'filter', 'in': 'query', 'required': False,
                               'style': 'deepObject', 'explode': True,
                               'schema': {'type': 'object', 'additionalProperties': False,
                                          'properties': {f.name: f.spec() for f in self.query}},
                               'description': 'unknown parameters are rejected'})
        if self.mutating and self.path:
            parameters.append({'name': 'Idempotency-Key', 'in': 'header', 'required': True,
                               'schema': {'type': 'string', 'maxLength': 128},
                               'description': 'one key, one answer; a repeat returns the first one'})
        if self.revisioned:
            parameters.append({'name': 'If-Match', 'in': 'header', 'required': False,
                               'schema': {'type': 'string'},
                               'description': 'revision of the object; a stale one gives 409'})
        out = {'operationId': self.operation, 'summary': self.summary, 'tags': list(self.tags),
               'parameters': parameters, 'responses': self.responses(),
               'security': [] if self.permission is None else [{'bearerKey': []}]}
        if self.anonymous:
            out['security'] = [{'bearerKey': []}, {}]
        if self.body:
            out['requestBody'] = {'required': True,
                                  'content': {'application/json': {
                                      'schema': {'type': 'object', 'additionalProperties': False,
                                                 'properties': {f.name: f.spec() for f in self.body}}}}}
        notes = []
        if self.mutating:
            notes.append('mutation: Idempotency-Key is required, If-Match or body revision '
                         'guards concurrent changes')
        if self.async_job:
            notes.append('long operation: 202 with a job id, the request is not held')
        if self.sse:
            notes.append('text/event-stream with the same cursor scheme as the listings')
        if self.legacy_token:
            notes.append('deprecated compatibility path: ?token= is accepted for reads only')
        if self.sensitive:
            notes.append('extra permission needed: ' + ', '.join(
                f'{name} requires {permission}' for name, permission in self.sensitive))
        if notes:
            out['description'] = '. '.join(notes)
        return out

    def responses(self):
        codes = ['400', '401', '403', '404', '429'] if not self.mutating else [
            '400', '401', '403', '404', '409', '413', '429']
        if self.sse:
            return {'200': {'description': 'event stream, text/event-stream'},
                    **{code: error_response() for code in codes}}
        success = '202' if self.async_job else '200'
        result = {success: {'description': 'job id and location' if self.async_job else 'result',
                            'content': {'application/json': {'schema': {'type': 'object'}}}}}
        if self.paginated:
            result['304'] = {'description': 'not modified, see ETag'}
        return {**result, **{code: error_response() for code in codes}}


def error_response():
    return {'description': 'documented error with a stable E_* code',
            'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Error'}}}}


PAGE_QUERY = (Field('cursor', 'string', max_len=512),
              Field('limit', 'int', minimum=1, maximum=MAX_PAGE_LIMIT))
COLLECTION_FIELD = Field('collection_id', 'string', max_len=128)


def canon_sorts():
    """Closed sort list, taken from the engine so API and CLI cannot diverge."""
    try:
        from .proxytool import SORTS
        return tuple(SORTS)
    except Exception:  # pragma: no cover - only when the engine is unavailable
        return ('recommended', 'quality', 'speed', 'stability', 'uptime', 'bandwidth')


def canon_protocols():
    try:
        from .proxytool import PROTOCOLS
        return tuple(PROTOCOLS)
    except Exception:  # pragma: no cover
        return ('all', 'http', 'https', 'socks4', 'socks5')


def sort_fields():
    return (Field('sort', 'string', choices=canon_sorts(), default=None),
            Field('max_age_seconds', 'int', minimum=0, maximum=30 * 86400),
            Field('include_unknown', 'bool'),
            Field('include_stale', 'bool'))


CHECK_BODY = (COLLECTION_FIELD,
              Field('profile_id', 'string', max_len=128),
              Field('profile_revision', 'int', minimum=1),
              Field('mode', 'string', choices=('collect_only', 'tcp', 'basic', 'full')),
              Field('want', 'int', minimum=1, maximum=100000),
              Field('max_seconds', 'int', minimum=1, maximum=86400),
              Field('budget', 'object', shape=(Field('max_requests', 'int', minimum=1),
                                               Field('max_bytes', 'int', minimum=1),
                                               Field('max_items', 'int', minimum=1),
                                               Field('max_seconds', 'int', minimum=1),
                                               Field('max_cost', 'float', minimum=0))),
              Field('notes', 'string', max_len=512))
KEY_BODY = (Field('name', 'string', required=True, max_len=128),
            Field('purpose', 'string', max_len=256),
            Field('permissions', 'list', required=True, max_items=32),
            Field('collections', 'list', max_items=256),
            Field('pools', 'list', max_items=256),
            Field('expires_at', 'float', minimum=0),
            Field('rate_limit_requests', 'int', minimum=1, maximum=100000),
            Field('rate_limit_window_seconds', 'int', minimum=1, maximum=86400),
            Field('concurrency', 'int', minimum=1, maximum=1024),
            Field('rotation_grace_seconds', 'int', minimum=0, maximum=86400, default=0))
SUBSCRIPTION_BODY = (Field('name', 'string', required=True, max_len=128),
                     Field('collection_id', 'string', max_len=128),
                     Field('expires_at', 'float', minimum=0),
                     Field('max_age_seconds', 'int', minimum=0, maximum=30 * 86400))

ROUTES = (
    # --- service ------------------------------------------------------------
    Route('GET', '/v1/service', 'service.state', 'read.status',
          'service version, queue and the pools allowed for this key', tags=('service',)),
    Route('GET', '/v1/version', 'service.version', None, 'product and API version',
          anonymous=True, tags=('service',)),
    Route('GET', '/v1/capabilities', 'service.capabilities', 'read.status',
          'what this installation supports, including the network model', tags=('service',)),
    Route('GET', '/v1/health', 'service.health', None, 'liveness without scope data',
          anonymous=True, tags=('service',)),
    Route('GET', '/v1/readiness', 'service.readiness', 'read.status',
          'readiness of the service layer', tags=('service',)),
    Route('GET', '/v1/queue', 'service.queue', 'read.status', 'queue depth and capacity',
          tags=('service',)),
    Route('GET', '/v1/audit', 'audit.list', 'admin.audit', 'local audit log of key operations',
          paginated=True, query=PAGE_QUERY, tags=('keys',)),

    # --- keys ---------------------------------------------------------------
    Route('GET', '/v1/keys', 'keys.list', 'admin.keys', 'list keys, never their secrets',
          paginated=True, query=PAGE_QUERY, tags=('keys',)),
    Route('POST', '/v1/keys', 'keys.create', 'admin.keys',
          'create a key; the secret is returned once and never again', mutating=True,
          body=KEY_BODY, tags=('keys',)),
    Route('GET', '/v1/keys/{id}', 'keys.get', 'admin.keys', 'one key with its metadata',
          tags=('keys',)),
    Route('PATCH', '/v1/keys/{id}', 'keys.update', 'admin.keys', 'rename or re-document a key',
          mutating=True, revisioned=True,
          body=(Field('name', 'string', max_len=128), Field('purpose', 'string', max_len=256),
                Field('expires_at', 'float', minimum=0),
                Field('revision', 'int', minimum=1)), tags=('keys',)),
    Route('POST', '/v1/keys/{id}/rotate', 'keys.rotate', 'admin.keys', 'rotate the secret of a key',
          mutating=True,
          body=(Field('rotation_grace_seconds', 'int', minimum=0, maximum=86400, default=0),),
          tags=('keys',)),
    Route('POST', '/v1/keys/{id}/revoke', 'keys.revoke', 'admin.keys',
          'revoke a key for good; its metadata and audit trail stay', mutating=True, body=(),
          tags=('keys',)),
    Route('POST', '/v1/keys/{id}/disable', 'keys.disable', 'admin.keys',
          'stop the use of a key without destroying it', mutating=True, body=(), tags=('keys',)),
    Route('POST', '/v1/keys/{id}/enable', 'keys.enable', 'admin.keys', 'enable a disabled key',
          mutating=True, body=(), tags=('keys',)),
    Route('DELETE', '/v1/keys/{id}', 'keys.delete', 'admin.keys', 'delete a revoked key',
          mutating=True, body=(), tags=('keys',)),
    Route('GET', '/v1/subscriptions', 'subscriptions.list', 'admin.keys', 'subscription secrets',
          paginated=True, query=PAGE_QUERY, tags=('keys',)),
    Route('POST', '/v1/subscriptions', 'subscriptions.create', 'admin.keys',
          'create a narrow read-only subscription secret with expiry', mutating=True,
          body=SUBSCRIPTION_BODY, tags=('keys',)),
    Route('POST', '/v1/subscriptions/{id}/revoke', 'subscriptions.revoke', 'admin.keys',
          'revoke a subscription secret', mutating=True, body=(), tags=('keys',)),

    # --- collections --------------------------------------------------------
    Route('GET', '/v1/collections', 'collections.list', 'collections.read',
          'collections inside the scope of the key', paginated=True, query=PAGE_QUERY,
          tags=('collections',)),
    Route('POST', '/v1/collections', 'collections.create', 'collections.write', 'create a collection',
          mutating=True,
          body=(Field('name', 'string', required=True, max_len=128),
                Field('kind', 'string', choices=('public', 'own'), default='own'),
                Field('allow_private', 'bool', default=False)), tags=('collections',)),
    Route('GET', '/v1/collections/{id}', 'collections.get', 'collections.read', 'one collection',
          scope=(('collection', 'id'),), tags=('collections',)),
    Route('PATCH', '/v1/collections/{id}', 'collections.update', 'collections.write',
          'rename or archive a collection', mutating=True, revisioned=True,
          scope=(('collection', 'id'),),
          body=(Field('name', 'string', max_len=128), Field('allow_private', 'bool'),
                Field('archived', 'bool'), Field('revision', 'int', minimum=1)),
          tags=('collections',)),
    Route('DELETE', '/v1/collections/{id}', 'collections.archive', 'collections.write',
          'archive a collection without deleting its members', mutating=True, revisioned=True,
          scope=(('collection', 'id'),), body=(Field('revision', 'int', minimum=1),),
          tags=('collections',)),
    Route('GET', '/v1/collections/{id}/members', 'collections.members', 'collections.read',
          'members of one collection', paginated=True, query=PAGE_QUERY,
          scope=(('collection', 'id'),), tags=('collections',)),
    Route('POST', '/v1/collections/{id}/members', 'collections.member_add', 'collections.write',
          'add an endpoint to a collection', mutating=True, scope=(('collection', 'id'),),
          body=(Field('endpoint', 'string', required=True, max_len=512),
                Field('origin', 'string', max_len=64, default='manual')), tags=('collections',)),
    Route('DELETE', '/v1/collections/{id}/members/{endpoint_id}', 'collections.member_remove',
          'collections.write', 'remove a member of one collection only', mutating=True,
          scope=(('collection', 'id'),), body=(), tags=('collections',)),
    Route('POST', '/v1/collections/{id}/imports/preview', 'imports.preview', 'import.read',
          'validate an import without changing anything', scope=(('collection', 'id'),),
          body=(Field('format', 'string', choices=('txt', 'uri', 'csv', 'json'), default='txt'),
                Field('content', 'string', required=True, max_len=8 * 1024 * 1024),
                Field('mapping', 'object', shape=(Field('endpoint', 'string', max_len=64),
                                                  Field('country', 'string', max_len=64)))),
          tags=('collections',)),
    Route('POST', '/v1/collections/{id}/imports', 'imports.commit', 'import.commit',
          'commit an import into one collection', mutating=True, async_job=True,
          scope=(('collection', 'id'),),
          body=(Field('format', 'string', choices=('txt', 'uri', 'csv', 'json'), default='txt'),
                Field('content', 'string', required=True, max_len=8 * 1024 * 1024),
                Field('mode', 'string', choices=('merge', 'replace'), default='merge'),
                Field('revision', 'int', minimum=1), Field('preview_digest', 'string', max_len=128)),
          tags=('collections',)),
    Route('POST', '/v1/collections/{id}/merge', 'collections.merge', 'collections.write',
          'merge the members of another collection into this one', mutating=True, async_job=True,
          scope=(('collection', 'id'),),
          body=(Field('from_collection_id', 'string', required=True, max_len=128),
                Field('revision', 'int', minimum=1)), tags=('collections',)),
    Route('POST', '/v1/collections/{id}/replace', 'collections.replace', 'collections.write',
          'replace the members of one collection', mutating=True, async_job=True,
          scope=(('collection', 'id'),),
          body=(Field('format', 'string', choices=('txt', 'uri', 'csv', 'json'), default='txt'),
                Field('content', 'string', required=True, max_len=8 * 1024 * 1024),
                Field('revision', 'int', minimum=1)), tags=('collections',)),

    # --- sources ------------------------------------------------------------
    Route('GET', '/v1/sources/catalog', 'sources.catalog', 'sources.read',
          'catalog with an honest support status per entry', tags=('sources',)),
    Route('GET', '/v1/sources', 'sources.list', 'sources.read', 'configured sources',
          paginated=True, query=PAGE_QUERY, tags=('sources',)),
    Route('GET', '/v1/sources/{id}', 'sources.get', 'sources.read', 'one source with provenance',
          tags=('sources',)),
    Route('POST', '/v1/sources', 'sources.create', 'sources.write', 'define a custom source',
          mutating=True,
          body=(Field('url', 'string', required=True, max_len=2048),
                Field('format', 'string', required=True,
                      choices=('text', 'json', 'csv', 'page_json', 'html')),
                Field('collection_id', 'string', max_len=128),
                Field('interval_minutes', 'int', minimum=1, maximum=10080),
                Field('max_bytes', 'int', minimum=1, maximum=1024 * 1024 * 1024)),
          tags=('sources',)),
    Route('PATCH', '/v1/sources/{id}', 'sources.update', 'sources.write',
          'update a source definition', mutating=True, revisioned=True,
          body=(Field('url', 'string', max_len=2048),
                Field('format', 'string', choices=('text', 'json', 'csv', 'page_json', 'html')),
                Field('interval_minutes', 'int', minimum=1, maximum=10080),
                Field('max_bytes', 'int', minimum=1, maximum=1024 * 1024 * 1024),
                Field('revision', 'int', minimum=1)), tags=('sources',)),
    Route('POST', '/v1/sources/{id}/enable', 'sources.enable', 'sources.write', 'enable a source',
          mutating=True, body=(), tags=('sources',)),
    Route('POST', '/v1/sources/{id}/disable', 'sources.disable', 'sources.write', 'disable a source',
          mutating=True, body=(), tags=('sources',)),
    Route('POST', '/v1/sources/{id}/refresh/preview', 'sources.refresh_preview', 'sources.read',
          'what a refresh would fetch, without fetching', body=(), tags=('sources',)),
    Route('POST', '/v1/sources/{id}/refresh', 'sources.refresh', 'sources.write',
          'schedule a refresh job', mutating=True, async_job=True,
          body=(Field('collection_id', 'string', max_len=128), Field('delta', 'bool'),
                Field('force', 'bool')), tags=('sources',)),
    Route('GET', '/v1/sources/{id}/refresh/{job_id}', 'sources.refresh_status', 'sources.read',
          'state of a refresh job', tags=('sources',)),

    # --- profiles -----------------------------------------------------------
    Route('GET', '/v1/profiles/presets', 'profiles.presets', 'profiles.read',
          'preset target sets available for a profile', tags=('profiles',)),
    Route('GET', '/v1/profiles', 'profiles.list', 'profiles.read', 'named profiles and revisions',
          paginated=True, query=PAGE_QUERY, tags=('profiles',)),
    Route('POST', '/v1/profiles', 'profiles.create', 'profiles.write', 'create a profile',
          mutating=True,
          body=(Field('name', 'string', required=True, max_len=128),
                Field('targets', 'list', required=True, min_items=1, max_items=64),
                Field('rule', 'string', choices=('all', 'any', 'at_least_k'), default='all'),
                Field('k', 'int', minimum=1, maximum=64),
                Field('max_age_seconds', 'int', minimum=0, maximum=30 * 86400),
                Field('connect_timeout_s', 'int', minimum=1, maximum=600),
                Field('timeout_s', 'int', minimum=1, maximum=600),
                Field('attempts', 'int', minimum=1, maximum=10),
                Field('max_bytes', 'int', minimum=1, maximum=1024 * 1024),
                Field('parent_id', 'string', max_len=128)), tags=('profiles',)),
    Route('GET', '/v1/profiles/{id}', 'profiles.get', 'profiles.read', 'one profile revision',
          tags=('profiles',)),
    Route('POST', '/v1/profiles/{id}/versions', 'profiles.update_version', 'profiles.write',
          'store a new revision of a profile', mutating=True,
          body=(Field('targets', 'list', max_items=64),
                Field('rule', 'string', choices=('all', 'any', 'at_least_k')),
                Field('k', 'int', minimum=1, maximum=64),
                Field('max_age_seconds', 'int', minimum=0, maximum=30 * 86400),
                Field('connect_timeout_s', 'int', minimum=1, maximum=600),
                Field('timeout_s', 'int', minimum=1, maximum=600),
                Field('attempts', 'int', minimum=1, maximum=10),
                Field('max_bytes', 'int', minimum=1, maximum=1024 * 1024),
                Field('revision', 'int', minimum=1)), tags=('profiles',)),
    Route('POST', '/v1/profiles/{id}/clone', 'profiles.clone', 'profiles.write', 'clone a profile',
          mutating=True, body=(Field('name', 'string', required=True, max_len=128),),
          tags=('profiles',)),
    Route('POST', '/v1/profiles/{id}/archive', 'profiles.archive', 'profiles.write',
          'archive a profile', mutating=True, body=(), tags=('profiles',)),
    Route('POST', '/v1/profiles/{id}/validate', 'profiles.validate', 'profiles.read',
          'validate a profile against the current rules', body=(), tags=('profiles',)),

    # --- checks and jobs ----------------------------------------------------
    Route('POST', '/v1/checks/collect', 'checks.collect', 'jobs.submit',
          'collect candidates as a job', mutating=True, async_job=True, body=CHECK_BODY,
          tags=('checks',)),
    Route('POST', '/v1/checks/check', 'checks.check', 'jobs.submit', 'check endpoints as a job',
          mutating=True, async_job=True, body=CHECK_BODY, tags=('checks',)),
    Route('POST', '/v1/checks/recheck', 'checks.recheck', 'jobs.submit', 'recheck results as a job',
          mutating=True, async_job=True, body=CHECK_BODY, tags=('checks',)),
    Route('POST', '/v1/checks/quick-test', 'checks.quick_test', 'jobs.submit',
          'quick test one endpoint as a job, under the common policy', mutating=True, async_job=True,
          body=(COLLECTION_FIELD, Field('endpoint', 'string', required=True, max_len=512),
                Field('profile_id', 'string', max_len=128),
                Field('max_seconds', 'int', minimum=1, maximum=3600)), tags=('checks',)),
    Route('GET', '/v1/jobs', 'jobs.list', 'jobs.read', 'jobs inside the scope of the key',
          paginated=True, query=PAGE_QUERY, tags=('jobs',)),
    Route('GET', '/v1/jobs/{id}', 'jobs.get', 'jobs.read', 'one job with progress and scope',
          tags=('jobs',)),
    Route('POST', '/v1/jobs/{id}/pause', 'jobs.pause', 'jobs.control', 'pause a job',
          mutating=True, body=(), tags=('jobs',)),
    Route('POST', '/v1/jobs/{id}/resume', 'jobs.resume', 'jobs.control',
          'resume a job without widening its scope', mutating=True, body=(), tags=('jobs',)),
    Route('POST', '/v1/jobs/{id}/cancel', 'jobs.cancel', 'jobs.control', 'cancel a job',
          mutating=True, body=(), tags=('jobs',)),
    Route('POST', '/v1/jobs/{id}/retry', 'jobs.retry', 'jobs.control', 'retry a job as a new job',
          mutating=True, async_job=True, body=(), tags=('jobs',)),
    Route('GET', '/v1/jobs/{id}/events', 'jobs.events', 'jobs.read', 'events of one job stream',
          sse=True, query=(Field('cursor', 'string', max_len=512),
                           Field('limit', 'int', minimum=1, maximum=MAX_PAGE_LIMIT)),
          tags=('jobs',)),
    Route('GET', '/v1/events', 'events.system', 'jobs.read', 'events of the system stream',
          sse=True, query=(Field('stream', 'string', max_len=128),
                           Field('cursor', 'string', max_len=512),
                           Field('limit', 'int', minimum=1, maximum=MAX_PAGE_LIMIT)),
          tags=('jobs',)),

    # --- results ------------------------------------------------------------
    Route('GET', '/v1/results', 'results.list', 'read.results',
          'results of one scope with cursor pagination, filters and sort', paginated=True,
          legacy_token=True,
          query=PAGE_QUERY + sort_fields() + (
              COLLECTION_FIELD, Field('profile_id', 'string', max_len=128),
              Field('profile_revision', 'int', minimum=1), Field('generation', 'string', max_len=128),
              Field('protocol', 'string', choices=canon_protocols(), default='all'),
              Field('country', 'string', max_len=64),
              Field('max_latency_ms', 'int', minimum=0, maximum=600000),
              Field('min_mbps', 'float', minimum=0),
              Field('anonymity', 'string',
                    choices=('any', 'transparent', 'anonymous', 'elite'), default='any'),
              Field('exclude_hosting', 'bool')), tags=('results',)),
    Route('GET', '/v1/results/random', 'results.random', 'read.results', 'random admissible results',
          legacy_token=True, query=sort_fields() + (Field('count', 'int', minimum=1, maximum=100,
                                                          default=1), COLLECTION_FIELD),
          tags=('results',)),
    Route('GET', '/v1/results/top', 'results.top', 'read.results', 'best results of one scope',
          query=sort_fields() + (Field('count', 'int', minimum=1, maximum=MAX_PAGE_LIMIT,
                                       default=10), COLLECTION_FIELD), tags=('results',)),
    Route('GET', '/v1/results/selection', 'results.selection', 'read.results',
          'results of an explicit selection, bound to its own scope',
          query=sort_fields() + (Field('endpoint_ids', 'string', max_len=8192),
                                 Field('selection_id', 'string', max_len=128), COLLECTION_FIELD),
          tags=('results',)),
    Route('GET', '/v1/results/{id}', 'results.detail', 'read.results.detail',
          'one result with its verdict, age and admission reason', tags=('results',)),
    Route('GET', '/v1/results/{id}/observations', 'results.observations', 'read.results.detail',
          'observations of one result', paginated=True, query=PAGE_QUERY, tags=('results',)),

    # --- pools --------------------------------------------------------------
    Route('GET', '/v1/pools', 'pools.list', 'pools.read', 'pools allowed for the key',
          paginated=True, query=PAGE_QUERY, tags=('pools',)),
    Route('POST', '/v1/pools', 'pools.create', 'pools.write', 'create a named pool',
          mutating=True,
          body=(Field('name', 'string', required=True, max_len=128), COLLECTION_FIELD,
                Field('profile_id', 'string', max_len=128),
                Field('desired', 'int', minimum=1, maximum=100000, default=10),
                Field('reserve', 'int', minimum=0, maximum=100000, default=0),
                Field('minimum', 'int', minimum=0, maximum=100000, default=1),
                Field('policy', 'object', shape=(
                    Field('max_age_seconds', 'int', minimum=0, maximum=30 * 86400),
                    Field('countries', 'list', max_items=64), Field('exclude_hosting', 'bool'),
                    Field('cooldown_s', 'int', minimum=0, maximum=86400),
                    Field('quota', 'object', shape=(Field('max_cost', 'float', minimum=0),
                                                    Field('max_requests', 'int', minimum=1)))))),
          tags=('pools',)),
    Route('GET', '/v1/pools/{id}', 'pools.get', 'pools.read', 'one pool with its deficit reason',
          scope=(('pool', 'id'),), tags=('pools',)),
    Route('PATCH', '/v1/pools/{id}', 'pools.update', 'pools.write',
          'change target, reserve or policy', mutating=True, revisioned=True,
          scope=(('pool', 'id'),),
          body=(Field('name', 'string', max_len=128),
                Field('desired', 'int', minimum=1, maximum=100000),
                Field('reserve', 'int', minimum=0, maximum=100000),
                Field('minimum', 'int', minimum=0, maximum=100000),
                Field('policy', 'object', shape=(
                    Field('max_age_seconds', 'int', minimum=0, maximum=30 * 86400),
                    Field('countries', 'list', max_items=64), Field('exclude_hosting', 'bool'),
                    Field('cooldown_s', 'int', minimum=0, maximum=86400))),
                Field('revision', 'int', minimum=1)), tags=('pools',)),
    Route('POST', '/v1/pools/{id}/start', 'pools.start', 'pools.write', 'start maintaining a pool',
          mutating=True, body=(), scope=(('pool', 'id'),), tags=('pools',)),
    Route('POST', '/v1/pools/{id}/pause', 'pools.pause', 'pools.write', 'pause a pool',
          mutating=True, body=(), scope=(('pool', 'id'),), tags=('pools',)),
    # A refill reads stored rows and rewrites membership: it is a local, bounded
    # operation, not a measurement.  It used to be declared `async_job` and answer
    # 202 without touching the pool, so the job id pointed at nothing.
    Route('POST', '/v1/pools/{id}/refill', 'pools.refill', 'pools.write',
          'refill this pool from its collection and report what it can serve',
          mutating=True, scope=(('pool', 'id'),),
          body=(Field('budget', 'object', shape=(Field('max_requests', 'int', minimum=1),
                                                  Field('max_seconds', 'int', minimum=1))),),
          tags=('pools',)),
    Route('POST', '/v1/pools/{id}/recheck', 'pools.recheck', 'pools.write',
          'measure the pool collection again as a job', mutating=True, async_job=True,
          body=(Field('protocol', 'string', choices=('all', 'http', 'https', 'socks4', 'socks5')),
                Field('max_seconds', 'int', minimum=1, maximum=86400)),
          scope=(('pool', 'id'),), tags=('pools',)),
    Route('GET', '/v1/pools/{id}/members', 'pools.members', 'pools.read', 'members of a pool',
          paginated=True, query=PAGE_QUERY, scope=(('pool', 'id'),), tags=('pools',)),
    Route('GET', '/v1/pools/{id}/status', 'pools.status', 'pools.read',
          'state, deficit reasons and next attempt', scope=(('pool', 'id'),), tags=('pools',)),

    # --- gateway ------------------------------------------------------------
    Route('GET', '/v1/gateway/bindings', 'gateway.bindings', 'gateway.read',
          'listener and client bindings to pools and generations', tags=('gateway',)),
    Route('POST', '/v1/gateway/bindings', 'gateway.bind', 'gateway.write', 'bind a listener to a pool',
          mutating=True, revisioned=True,
          body=(Field('listener', 'string', required=True, max_len=128),
                Field('pool_id', 'string', required=True, max_len=128),
                Field('profile_id', 'string', max_len=128),
                Field('generation', 'string', max_len=128),
                Field('session_ttl_s', 'int', minimum=1, maximum=86400),
                Field('revision', 'int', minimum=1)), scope=(('pool', 'pool_id'),),
          tags=('gateway',)),
    Route('GET', '/v1/gateway/listeners', 'gateway.listeners', 'gateway.read',
          'listeners and their bind address', tags=('gateway',)),
    Route('GET', '/v1/gateway/sessions', 'gateway.sessions', 'gateway.read', 'session metadata',
          paginated=True, query=PAGE_QUERY, tags=('gateway',)),
    Route('GET', '/v1/gateway/config', 'gateway.config_get', 'gateway.read',
          'transport and concurrency configuration', tags=('gateway',)),
    Route('PATCH', '/v1/gateway/config', 'gateway.config_set', 'gateway.write',
          'change transport and concurrency configuration; bind addresses stay bounded',
          mutating=True, revisioned=True,
          body=(Field('listen_host', 'string', max_len=64),
                Field('listen_port', 'int', minimum=0, maximum=65535),
                Field('transports', 'list', max_items=8),
                Field('max_per_proxy', 'int', minimum=1, maximum=4096),
                Field('connect_timeout_s', 'int', minimum=1, maximum=600),
                Field('handshake_deadline_s', 'int', minimum=1, maximum=600),
                Field('session_ttl_s', 'int', minimum=1, maximum=86400),
                Field('revision', 'int', minimum=1)), guard='gateway_bind', tags=('gateway',)),

    # --- schedules ----------------------------------------------------------
    Route('GET', '/v1/schedules', 'schedules.list', 'schedules.read', 'schedules',
          paginated=True, query=PAGE_QUERY, tags=('schedules',)),
    Route('POST', '/v1/schedules', 'schedules.create', 'schedules.write', 'create a schedule',
          mutating=True, scope=(('pool', 'pool_id'),),
          body=(Field('name', 'string', required=True, max_len=128),
                Field('kind', 'string', required=True,
                      choices=('check', 'refill', 'recheck', 'export', 'source')),
                Field('pool_id', 'string', max_len=128), COLLECTION_FIELD,
                Field('interval_minutes', 'float', minimum=0.1, maximum=10080, required=True),
                Field('window', 'object', shape=(Field('from', 'string', max_len=16),
                                                 Field('to', 'string', max_len=16),
                                                 Field('timezone', 'string', max_len=64))),
                Field('quiet_hours', 'object', shape=(Field('from', 'string', max_len=16),
                                                      Field('to', 'string', max_len=16))),
                Field('budgets', 'object', shape=(Field('max_requests', 'int', minimum=1),
                                                  Field('max_bytes', 'int', minimum=1),
                                                  Field('max_seconds', 'int', minimum=1))),
                Field('enabled', 'bool', default=True)), tags=('schedules',)),
    Route('GET', '/v1/schedules/{id}', 'schedules.get', 'schedules.read', 'one schedule',
          tags=('schedules',)),
    Route('PATCH', '/v1/schedules/{id}', 'schedules.update', 'schedules.write', 'update a schedule',
          mutating=True, revisioned=True,
          body=(Field('name', 'string', max_len=128),
                Field('interval_minutes', 'float', minimum=0.1, maximum=10080),
                Field('window', 'object', shape=(Field('from', 'string', max_len=16),
                                                 Field('to', 'string', max_len=16),
                                                 Field('timezone', 'string', max_len=64))),
                Field('budgets', 'object', shape=(Field('max_requests', 'int', minimum=1),
                                                  Field('max_bytes', 'int', minimum=1),
                                                  Field('max_seconds', 'int', minimum=1))),
                Field('revision', 'int', minimum=1)), tags=('schedules',)),
    Route('DELETE', '/v1/schedules/{id}', 'schedules.delete', 'schedules.write', 'delete a schedule',
          mutating=True, body=(), tags=('schedules',)),
    Route('POST', '/v1/schedules/{id}/enable', 'schedules.enable', 'schedules.write',
          'enable a schedule', mutating=True, body=(), tags=('schedules',)),
    Route('POST', '/v1/schedules/{id}/disable', 'schedules.disable', 'schedules.write',
          'disable a schedule', mutating=True, body=(), tags=('schedules',)),
    Route('GET', '/v1/schedules/{id}/next-run', 'schedules.next_run', 'schedules.read',
          'next run with its timezone and window', tags=('schedules',)),
    Route('GET', '/v1/schedules/{id}/counters', 'schedules.counters', 'schedules.read',
          'counters of the last runs', tags=('schedules',)),

    # --- exports ------------------------------------------------------------
    Route('POST', '/v1/exports', 'exports.create', 'export.create',
          'create an export artifact as a job', mutating=True, async_job=True,
          body=(COLLECTION_FIELD, Field('profile_id', 'string', max_len=128),
                Field('profile_revision', 'int', minimum=1),
                Field('kind', 'string', default='selection',
                      choices=('published', 'selection', 'diagnostic')),
                Field('format', 'string', default='json',
                      choices=('json', 'txt', 'csv', 'pac', 'clash', 'singbox')),
                Field('endpoint_ids', 'string', max_len=8192),
                Field('include_secrets', 'bool', default=False)),
          sensitive=(('include_secrets', 'export.secret'),), tags=('exports',)),
    Route('GET', '/v1/exports/{id}', 'exports.status', 'read.export.artifact',
          'state of an export artifact', tags=('exports',)),
    Route('GET', '/v1/exports/{id}/compatibility', 'exports.compatibility', 'read.export.artifact',
          'which rows the target client cannot use and why', tags=('exports',)),
    Route('GET', '/v1/exports/{id}/download/{name}', 'exports.download', 'read.export.artifact',
          'download one file of the scope this artifact was created for',
          query=(Field('include_secrets', 'bool', default=False),), raw_body=True,
          sensitive=(('include_secrets', 'export.secret'),), tags=('exports',)),

    # --- reservations -------------------------------------------------------
    Route('POST', '/v1/reservations/acquire', 'reservations.acquire', 'pools.read',
          'take a lease on pool members with a TTL and a bounded count', mutating=True,
          scope=(('pool', 'pool_id'),),
          body=(Field('pool_id', 'string', required=True, max_len=128),
                Field('count', 'int', minimum=1, maximum=64, default=1),
                Field('ttl_s', 'int', minimum=1, maximum=86400, required=True),
                Field('profile_id', 'string', max_len=128),
                Field('max_age_seconds', 'int', minimum=0, maximum=30 * 86400),
                Field('exclude_hosting', 'bool', default=False)), tags=('reservations',)),
    Route('POST', '/v1/reservations/lease', 'reservations.lease', 'pools.read',
          'renew an existing lease', mutating=True, scope=(('pool', 'pool_id'),),
          body=(Field('lease_id', 'string', required=True, max_len=128),
                Field('pool_id', 'string', required=True, max_len=128),
                Field('ttl_s', 'int', minimum=1, maximum=86400, required=True)),
          tags=('reservations',)),
    Route('POST', '/v1/reservations/release', 'reservations.release', 'pools.read',
          'release a lease', mutating=True, scope=(('pool', 'pool_id'),),
          body=(Field('lease_id', 'string', required=True, max_len=128),
                Field('pool_id', 'string', required=True, max_len=128),
                Field('state', 'string', choices=('returned', 'lost', 'consumed'))),
          tags=('reservations',)),
    Route('POST', '/v1/reservations/feedback', 'reservations.feedback', 'pools.write',
          'bounded target-aware feedback; it never changes global reputation', mutating=True,
          scope=(('pool', 'pool_id'),),
          body=(Field('pool_id', 'string', required=True, max_len=128),
                Field('endpoint_id', 'string', required=True, max_len=128),
                Field('ok', 'bool', required=True),
                Field('latency_ms', 'int', minimum=0, maximum=600000),
                Field('target_id', 'string', max_len=128),
                Field('error_code', 'string', max_len=64)), tags=('reservations',)),
)


# Key management belongs to the key store, not to the service layer (CONTRACTS 5.2).
KEY_METHODS = {
    'keys.list': 'list_keys', 'keys.get': 'get_key', 'keys.create': 'create_key',
    'keys.update': 'update_key', 'keys.rotate': 'rotate_key', 'keys.revoke': 'revoke_key',
    'keys.disable': 'disable_key', 'keys.enable': 'enable_key', 'keys.delete': 'delete_key',
    'subscriptions.list': 'list_keys', 'subscriptions.create': 'create_key',
    'subscriptions.revoke': 'revoke_key', 'audit.list': 'read_audit',
}
# A subscription secret is a key whose rights the API narrows, never widens.
SUBSCRIPTION_PURPOSE = 'subscription'


def _check_route_table():
    """Invariants that must hold before a single request is served."""
    seen = set()
    for route in ROUTES:
        if not route.path.startswith(API_PREFIX + '/'):
            raise AssertionError(f'{route.path} is outside {API_PREFIX}')
        if route.permission is not None and route.permission not in PERMISSIONS:
            raise AssertionError(f'{route.operation}: unknown permission {route.permission}')
        if route.method == 'GET' and (route.mutating or route.async_job):
            raise AssertionError(f'{route.operation}: a GET must not mutate or start a job')
        if route.sse and route.method != 'GET':
            raise AssertionError(f'{route.operation}: an event stream is a GET')
        if (route.method, route.path) in seen:
            raise AssertionError(f'duplicate route {route.method} {route.path}')
        seen.add((route.method, route.path))
        if route.scope and route.scope[0][0] not in ('collection', 'pool'):
            raise AssertionError(f'{route.operation}: unknown scope kind')


_check_route_table()


def _path_params(route, wanted):
    """Path parameters of a route, or None when the path does not match at all."""
    parts = route.path[len(API_PREFIX):].strip('/').split('/')
    if len(parts) != len(wanted):
        return None
    params = {}
    for spec, value in zip(parts, wanted):
        if spec.startswith('{'):
            if not ID_PATTERN.match(value):
                return None
            params[spec[1:-1]] = value
        elif spec != value:
            return None
    return params


def match_route(method, path):
    """Exact method and path, or (None, {}).  Static segments win over parameters."""
    if not path.startswith(API_PREFIX):
        return None, {}
    wanted = path[len(API_PREFIX):].strip('/').split('/')
    for route in ROUTES:
        if route.method != method:
            continue
        params = _path_params(route, wanted)
        if params is not None:
            return route, params
    return None, {}


def allowed_methods(path):
    """Methods a path accepts, for the Allow header of a 405."""
    if not path.startswith(API_PREFIX):
        return []
    wanted = path[len(API_PREFIX):].strip('/').split('/')
    return sorted({route.method for route in ROUTES if _path_params(route, wanted) is not None})


# --------------------------------------------------------------------------
# the API
# --------------------------------------------------------------------------

def _redact(value, principal):
    """A subscription or legacy identity never sees private fields."""
    if principal is None or not principal.redacted:
        return value
    if isinstance(value, dict):
        return {k: _redact(v, principal) for k, v in value.items() if k not in REDACTED_FIELDS}
    if isinstance(value, list):
        return [_redact(item, principal) for item in value]
    return value


#: The envelopes a service answer may put the object in.  The guard looks inside
#: them: the three artifact reads and every list answer `{"item": {...}}` or
#: `{"items": [...]}`, so a guard that only inspected the top level saw no
#: `collection_id` at all and let another collection's object through (F29).
#: The list is explicit -- the walk is one level deep on purpose, not a search.
SCOPE_ENVELOPES = ('item', 'items')


def _names_out_of_scope(principal, body):
    """True when the answer names a collection or a pool the key does not cover."""
    if not isinstance(body, dict):
        return False
    for kind, key in (('collection', 'collection_id'), ('pool', 'pool_id')):
        value = body.get(key)
        if isinstance(value, str) and not principal.allows(kind, value):
            return True
    scope = body.get('scope')
    if isinstance(scope, dict):
        for kind, key in (('collection', 'collection_id'), ('pool', 'pool_id')):
            value = scope.get(key)
            if isinstance(value, str) and not principal.allows(kind, value):
                return True
    return False


def _guard_scope(principal, body):
    """Backstop: a response naming an out-of-scope object is not returned.

    One object out of scope answers 404 -- the same code a missing one gives, so a
    caller cannot probe for what it may not see.  A *list* keeps the rows it may
    see and loses the rest: refusing the whole page would also refuse the caller's
    own objects, and the acceptance item only asks that the other collection's
    objects do not arrive.
    """
    if not isinstance(body, dict) or principal is None:
        return body
    if _names_out_of_scope(principal, body):
        raise ApiError('E_STATE_NOT_FOUND', status=404,
                       action=tr('объект вне scope ключа', 'the object is outside the key scope'))
    for name in SCOPE_ENVELOPES:
        inner = body.get(name)
        if isinstance(inner, dict):
            _guard_scope(principal, inner)
        elif isinstance(inner, list):
            body[name] = [item for item in inner
                          if not _names_out_of_scope(principal, item)]
    return body


class ApiV1:
    """Transport independent core of the control API.

    :meth:`handle` is the whole request pipeline; :meth:`make_server` only adds
    an HTTP server around it, so the same behaviour is testable over a socket
    and without one.
    """

    def __init__(self, service, keys, config=None, clock=None):
        require_object(service, Service.REQUIRED, 'service')
        require_object(keys, KeyStore.REQUIRED, 'keys')
        self.config = config or ApiConfig()
        self.clock = clock or time.time
        self._check_config()
        self.service = service
        self.keys = keys
        self.rate = RateLimiter(self.clock)
        self.slots = ConcurrencyLimiter(self.config.max_concurrency)
        self.idempotency = IdempotencyStore(self.config.idempotency_ttl_s,
                                            self.config.idempotency_max_entries, self.clock)
        self.history = EventHistory(self.config.max_event_history)
        self.legacy_deprecations = 0
        self.stats = {'requests': 0, 'denied': 0, 'rate_limited': 0, 'jobs_submitted': 0}

    # -- public API ---------------------------------------------------------

    def handle(self, request):
        """Answer one request.  Returns a :class:`Response`; never raises ApiError."""
        self.stats['requests'] += 1
        try:
            return self._handle(request)
        except ApiError as exc:
            if exc.status in (401, 403, 429):
                self.stats['denied'] += 1
                self._audit_denial(request, exc)
            if exc.code == 'E_AUTH_RATE_LIMITED':
                self.stats['rate_limited'] += 1
            return self._error(request, exc)

    def _check_config(self):
        """A network bind the Host check cannot serve is refused at construction."""
        if not self.config.loopback_only and not self.config.allowed_hosts:
            raise ValueError(tr(
                f'API на {self.config.host} доступно из сети: задайте allowed_hosts, '
                'иначе проверка Host закроет всё.',
                f'the API on {self.config.host} is reachable from the network: set allowed_hosts, '
                'otherwise the Host check rejects everything'))

    def make_server(self, config=None):
        """A threading HTTP server around :meth:`handle`."""
        if config is not None:
            self.config = config
            self._check_config()
        api = self

        class Handler(BaseHTTPRequestHandler):
            server_version = f'{PRODUCT_NAME}/{PRODUCT_VERSION}'
            sys_version = ''
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def do_GET(self):
                self._dispatch('GET')

            def do_HEAD(self):
                self._dispatch('HEAD')

            def do_POST(self):
                self._dispatch('POST')

            def do_PATCH(self):
                self._dispatch('PATCH')

            def do_DELETE(self):
                self._dispatch('DELETE')

            def do_OPTIONS(self):
                self._dispatch('OPTIONS')

            def _dispatch(self, method):
                url = urlsplit(self.path)
                try:
                    length = int(self.headers.get('Content-Length') or 0)
                except ValueError:
                    length = 0
                # an oversized body is refused without being buffered in full
                capped = min(length, api.config.max_body_bytes + 1)
                body = self.rfile.read(capped) if capped else b''
                if length > capped:
                    self.close_connection = True
                response = api.handle(Request(method=method, path=url.path, query=url.query,
                                              headers=dict(self.headers.items()), body=body,
                                              client_host=self.client_address[0]))
                self._send(response, head_only=method == 'HEAD')

            def _send(self, response, head_only=False):
                self.send_response(response.status)
                self.send_header('Content-Type', response.content_type)
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Workbench-Api-Version', API_VERSION)
                for name, value in response.headers:
                    self.send_header(name, value)
                if response.stream is None:
                    self.send_header('Content-Length', str(len(response.body)))
                    self.end_headers()
                    if not head_only:
                        self.wfile.write(response.body)
                    return
                self.send_header('Transfer-Encoding', 'chunked')
                self.end_headers()
                if head_only:
                    self.wfile.write(b'0\r\n\r\n')
                    return
                for frame in response.stream:
                    payload = frame.encode('utf-8')
                    self.wfile.write(b'%x\r\n' % len(payload) + payload + b'\r\n')
                    self.wfile.flush()
                self.wfile.write(b'0\r\n\r\n')
                self.wfile.flush()

        server = ThreadingHTTPServer((self.config.host, self.config.port), Handler)
        server.daemon_threads = True
        if self.config.tls_cert:
            try:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                context.load_cert_chain(self.config.tls_cert, self.config.tls_key)
                server.socket = context.wrap_socket(server.socket, server_side=True)
            except Exception:
                server.server_close()
                raise
        return server

    def openapi(self):
        """The document generated from the single description of the routes."""
        return openapi_document()

    # -- pipeline -----------------------------------------------------------

    def _handle(self, request):
        method = 'GET' if request.method == 'HEAD' else request.method
        self._check_network(request)
        if method == 'OPTIONS':
            return self._preflight(request)
        if not request.path.startswith(API_PREFIX):
            raise ApiError('E_STATE_NOT_FOUND', status=404, details={'path': request.path},
                           action=tr('версионированный API живёт под /v1',
                                     'the versioned API lives under /v1'))
        route, params = match_route(method, request.path)
        if route is None:
            raise self._method_error(method, request.path)
        if len(request.body) > self.config.max_body_bytes:
            raise ApiError('E_LIMIT_BODY', status=413,
                           details={'max_body_bytes': self.config.max_body_bytes,
                                    'body_bytes': len(request.body)},
                           action=tr('разбейте запрос на части', 'split the request'))
        principal, legacy_used = self._authenticate(request, route)
        if legacy_used:
            self.legacy_deprecations += 1
        self._rate_check(principal, request, route)
        if route.permission is not None:
            require_permission(principal, route.permission, self.clock())
        for kind, source in route.scope:
            require_scope(principal, kind, params.get(source))
        idem_key = request.header('Idempotency-Key')
        if route.mutating and self.config.require_idempotency_key and not idem_key:
            raise field_error('Idempotency-Key', tr('обязателен для изменяющих запросов',
                                                    'required for mutations'),
                              action=tr('повторите запрос с тем же ключом после сбоя',
                                        'repeat the request with the same key after a failure'))
        if route.sse:
            return self._stream(request, route, params, principal)
        return self._invoke(request, route, params, principal, idem_key)

    def _method_error(self, method, path):
        allowed = allowed_methods(path)
        if not allowed:
            return ApiError('E_STATE_NOT_FOUND', status=404, details={'path': path})
        return ApiError('E_VALIDATION_METHOD', status=405,
                        details={'method': method, 'allowed': allowed},
                        allow=','.join(allowed))

    def _check_network(self, request):
        """Host, Origin and CORS follow the real model, not convenience."""
        host = (request.header('Host') or '').strip()
        if self.config.allowed_hosts:
            name = host.rsplit(':', 1)[0].strip('[]').lower()
            if name not in {allowed.lower() for allowed in self.config.allowed_hosts}:
                raise ApiError('E_AUTH_ORIGIN', status=403, details={'host': name},
                               action=tr('добавьте это имя в allowed_hosts',
                                         'add this name to allowed_hosts'))
        elif self.config.loopback_only and not is_loopback_host(host):
            raise ApiError('E_AUTH_ORIGIN', status=403, details={'host': host},
                           action=tr('локальный API принимает только loopback Host',
                                     'the local API accepts a loopback Host only'))
        origin = (request.header('Origin') or '').strip()
        if origin and origin not in self.config.allowed_origins:
            raise ApiError('E_AUTH_ORIGIN', status=403, details={'origin': origin},
                           action=tr('добавьте Origin в allowed_origins',
                                     'add the Origin to allowed_origins'))

    def _cors_headers(self, request):
        origin = (request.header('Origin') or '').strip()
        if not origin or origin not in self.config.cors_origins:
            return ()
        return (('Access-Control-Allow-Origin', origin),
                ('Access-Control-Allow-Methods', 'GET, POST, PATCH, DELETE, OPTIONS'),
                ('Access-Control-Allow-Headers',
                 'Authorization, Content-Type, Idempotency-Key, If-Match, If-None-Match'),
                ('Access-Control-Expose-Headers', 'ETag, Location, Retry-After, Warning'),
                ('Access-Control-Max-Age', '600'), ('Vary', 'Origin'))

    def _preflight(self, request):
        origin = (request.header('Origin') or '').strip()
        headers = self._cors_headers(request)
        if not headers:
            raise ApiError('E_AUTH_ORIGIN', status=403, details={'origin': origin},
                           action=tr('этот Origin не разрешён', 'this Origin is not allowed'))
        return Response(204, b'', 'text/plain; charset=utf-8', headers)

    def _authenticate(self, request, route):
        """Keys travel in a header.  A query token stays a deprecated read path."""
        secret = ''
        header = request.header('Authorization') or ''
        if header:
            scheme, _, value = header.partition(' ')
            if scheme.lower() != 'bearer' or not value.strip():
                raise ApiError('E_AUTH_INVALID',
                               action=tr('используйте Authorization: Bearer <key>',
                                         'use Authorization: Bearer <key>'))
            secret = value.strip()
        legacy_used = False
        legacy_allowed = route.legacy_token and self.config.allow_query_token
        if not secret and legacy_allowed:
            supplied = (parse_qs(request.query).get('token') or [''])[-1]
            if supplied:
                secret = supplied
                legacy_used = True
        if not secret:
            if route.anonymous and self.config.loopback_only:
                return None, False
            if route.anonymous:
                raise ApiError('E_AUTH_MISSING', action=tr(
                    'на сетевом bind даже /v1/version требует ключа',
                    'on a network bind even /v1/version needs a key'))
            raise ApiError('E_AUTH_MISSING')
        principal = self.keys.verify(secret)
        if principal is None:
            raise ApiError('E_AUTH_INVALID', status=401)
        principal.check_time(self.clock())
        if not route.anonymous:
            self._check_identity(principal, route)
        return principal, legacy_used

    def _check_identity(self, principal, route):
        """A narrow identity is refused by the kind of operation, not by name."""
        if principal.kind == 'subscription' and (route.mutating or
                                                  route.permission not in READ_PERMISSIONS):
            raise ApiError('E_AUTH_PERMISSION', status=403, details={'kind': 'subscription'},
                           action=tr('секрет подписки только читает результаты',
                                     'a subscription secret only reads results'))
        if principal.kind == 'legacy' and (
                route.mutating or route.permission not in LEGACY_READ_PERMISSIONS):
            raise ApiError('E_AUTH_PERMISSION', status=403, details={'kind': 'legacy'},
                           action=tr('legacy-токен остаётся read-only',
                                     'the legacy token stays read-only'))

    def _rate_check(self, principal, request, route):
        limit, window = self.config.rate_limit
        bucket = f"client:{request.client_host}"
        if principal is not None:
            if principal.rate_limit:
                limit, window = principal.rate_limit
            bucket = principal.key_id
        self.rate.check(f'{bucket}:{route.permission or "anonymous"}', limit, window)

    def _invoke(self, request, route, params, principal, idem_key):
        extra_headers = ()
        if route.legacy_token and self.config.allow_query_token and \
                (parse_qs(request.query).get('token') or [''])[-1]:
            extra_headers = (('Deprecation', 'true'),
                             ('Warning', '299 - "a token in the query string is deprecated; '
                                         'send Authorization: Bearer instead"'))
        digest = digest_of({'path': request.path, 'query': request.query,
                            'body': request.body.decode('utf-8', 'replace')})
        bucket = (principal.key_id if principal else 'anonymous', request.method, request.path)
        if idem_key:
            cached = self.idempotency.get(bucket, idem_key, digest)
            if cached is not None:
                return cached
        body = self._parse_body(request, route)
        query = self._parse_query(route, request)
        self._check_sensitive(route, principal, body, query)
        self._apply_guard(route, body)
        expected = self._expected_revision(request, body, route)
        call = Call(operation=route.operation, principal=principal, params=params,
                    query=query, body=body, idempotency_key=idem_key,
                    expected_revision=expected, deadline_s=self.config.request_timeout_s,
                    path=request.path, method=request.method)
        self.slots.acquire()
        try:
            if route.async_job:
                self._check_queue()
            result = self._key_call(route, call, principal) \
                if route.operation in KEY_METHODS else self.service.invoke(route.operation, call)
        except ApiError as exc:
            self._audit(route, request, params, principal, 'error', exc.code, body)
            raise
        except Exception as exc:
            refusal = _domain_refusal(exc)
            if refusal is not None:
                self._audit(route, request, params, principal, 'error', refusal.code, body)
                raise refusal from exc
            self._audit(route, request, params, principal, 'error', 'E_SERVICE_UNAVAILABLE', body)
            raise ApiError('E_SERVICE_UNAVAILABLE',
                           status=503 if isinstance(exc, NotImplementedError) else 500,
                           details={'reason': type(exc).__name__},
                           action=tr('операция не подключена к сервисному слою',
                                     'the operation is not wired to the service layer')) from exc
        finally:
            self.slots.release()
        response = self._response(route, request, result, principal, extra_headers, query)
        if idem_key:
            self.idempotency.put(bucket, idem_key, digest, response)
        self._audit(route, request, params, principal, 'ok', None, body)
        if route.async_job:
            self.stats['jobs_submitted'] += 1
        return response

    def _key_call(self, route, call, principal):
        """Key management is answered by the key store, with the API fixing the rights."""
        method = KEY_METHODS[route.operation]
        key_id = call.params.get('id')
        if route.operation == 'keys.create':
            return self.keys.create_key(principal, dict(call.body, purpose=call.body.get('purpose')))
        if route.operation == 'subscriptions.create':
            spec = dict(call.body, permissions=sorted(SUBSCRIPTION_PERMISSIONS),
                        purpose=SUBSCRIPTION_PURPOSE, collections=[call.body['collection_id']]
                        if call.body.get('collection_id') else [])
            return self.keys.create_key(principal, spec)
        if route.operation == 'keys.update':
            return self.keys.update_key(principal, key_id,
                                        {k: v for k, v in call.body.items() if k != 'revision'})
        if route.operation == 'keys.rotate':
            return self.keys.rotate_key(principal, key_id,
                                        grace_s=call.body.get('rotation_grace_seconds') or 0.0)
        if route.operation == 'subscriptions.list':
            page = self.keys.list_keys(principal, limit=call.query.get('limit'))
            page = dict(page, items=[item for item in page.get('items', [])
                                     if item.get('purpose') == SUBSCRIPTION_PURPOSE])
            return page
        if route.operation == 'keys.list':
            return self.keys.list_keys(principal, limit=call.query.get('limit'))
        if route.operation == 'audit.list':
            return self.keys.read_audit(principal, limit=call.query.get('limit'))
        if route.operation == 'keys.get':
            return self.keys.get_key(principal, key_id)
        return getattr(self.keys, method)(principal, key_id)

    def _check_queue(self):
        state = self.service.queue_state()
        if not isinstance(state, dict):
            raise ApiError('E_SERVICE_UNAVAILABLE', status=503,
                           details={'reason': 'queue_state is not an object'})
        depth = int(state.get('depth') or 0)
        capacity = state.get('capacity')
        if depth >= self.config.max_queue_depth or (capacity is not None and depth >= int(capacity)):
            raise ApiError('E_LIMIT_QUEUE', status=429, retry_after=5,
                           details={'depth': depth, 'max_queue_depth': self.config.max_queue_depth,
                                    'capacity': capacity},
                           action=tr('дождитесь завершения заданий', 'wait for jobs to finish'))

    def _parse_body(self, request, route):
        if not request.body:
            return validate_object(route.body, {})
        content_type = (request.header('Content-Type') or '').split(';')[0].strip()
        if content_type and content_type != 'application/json':
            raise ApiError('E_VALIDATION_SCHEMA', details={'content_type': content_type},
                           action=tr('тело должно быть application/json',
                                     'the body must be application/json'))
        try:
            data = json.loads(request.body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ApiError('E_VALIDATION_SCHEMA', details={'reason': 'invalid json'}) from exc
        return validate_object(route.body, data)

    def _parse_query(self, route, request):
        extra = ('token',) if route.legacy_token else ()
        query = parse_query_fields(route.query, request.query, extra=extra)
        query.pop('token', None)
        if 'limit' in query and query['limit'] > self.config.max_limit:
            raise field_error('limit', f'<= {self.config.max_limit}')
        if query.get('cursor'):
            stream_id, seq, bound = decode_cursor(query['cursor'])
            if bound and bound != self.filter_digest(request.path, query):
                raise ApiError('E_CONFLICT_REVISION', status=409,
                               details={'cursor': 'bound to another filter set'},
                               action=tr('\u0444\u0438\u043b\u044c\u0442\u0440 \u0438\u0437\u043c\u0435\u043d\u0438\u043b\u0441\u044f: \u043d\u0430\u0447\u043d\u0438\u0442\u0435 \u0432\u044b\u0434\u0430\u0447\u0443 \u0437\u0430\u043d\u043e\u0432\u043e \u0431\u0435\u0437 \u043a\u0443\u0440\u0441\u043e\u0440\u0430',
                                         'the filter changed: restart the listing without a cursor'))
            query['cursor_stream'] = stream_id
            query['cursor_seq'] = seq
        return query

    def filter_digest(self, path, query):
        """Identity of a listing: the path and the filters, not the page size."""
        return digest_of({'path': path,
                          'filter': {k: v for k, v in query.items()
                                     if k not in ('cursor', 'limit', 'cursor_stream',
                                                  'cursor_seq')}})[:16]

    def _check_sensitive(self, route, principal, body, query):
        """A sensitive option is a separate permission, never a parameter of convenience."""
        for name, permission in route.sensitive:
            if body.get(name) is True or query.get(name) is True:
                require_permission(principal, permission, self.clock())

    def _apply_guard(self, route, body):
        """Keep one operation inside the bounds of the network model."""
        if route.guard != 'gateway_bind':
            return
        host = body.get('listen_host')
        if host and not is_loopback_host(host) and host not in self.config.allowed_bind_hosts:
            raise field_error('listen_host', tr('адрес привязки не разрешён', 'bind address not allowed'),
                              action=tr('укажите loopback или адрес из allowed_bind_hosts',
                                        'use loopback or an address from allowed_bind_hosts'))

    def _expected_revision(self, request, body, route):
        declared = body.get('revision')
        header = (request.header('If-Match') or '').strip()
        from_header = None
        if header:
            cleaned = header.removeprefix('W/').strip('"')
            tail = re.sub(r'^[A-Za-z]+-', '', cleaned)
            if not tail.isdigit():
                raise field_error('If-Match', tr('ожидается номер ревизии', 'a revision number'),
                                  action=tr('перечитайте объект и повторите',
                                            'read the object and retry'))
            from_header = int(tail)
        if declared is not None and from_header is not None and int(declared) != from_header:
            raise field_error('revision', tr('расходится с If-Match', 'differs from If-Match'),
                              action=tr('приведите оба значения к одному', 'make both values equal'))
        revision = declared if declared is not None else from_header
        if route.revisioned and revision is None:
            raise field_error('If-Match', tr('обязателен для этого изменения',
                                              'required for this mutation'),
                              action=tr('укажите текущую ревизию объекта',
                                        'supply the current revision of the object'))
        return revision

    def _response(self, route, request, result, principal, extra_headers, query=None):
        headers = list(extra_headers)
        if route.raw_body:
            if not isinstance(result, dict) or 'data' not in result:
                raise ApiError('E_SERVICE_UNAVAILABLE', status=503,
                               details={'operation': route.operation, 'reason': 'no file'},
                               action=tr('сервисный слой вернул не файл',
                                         'the service layer did not return a file'))
            data = result['data']
            if not isinstance(data, (bytes, bytearray)):
                data = str(data).encode('utf-8')
            # The object guard runs before the bytes leave.  A raw file used to be
            # returned straight out of `result`, so a redacted identity (a
            # subscription) got a file the rest of the API would have scrubbed, and
            # a body naming another collection was never noticed at all (F29).
            _guard_scope(principal, result if isinstance(result, dict) else {})
            if result.get('filename'):
                headers.append(('Content-Disposition',
                                'attachment; filename='
                                + f'"{FILE_CHARS.sub("_", str(result["filename"]))}"'))
            return Response(200, bytes(data),
                            str(result.get('content_type') or 'application/octet-stream'),
                            tuple(headers))
        if not isinstance(result, dict):
            raise ApiError('E_SERVICE_UNAVAILABLE', status=503,
                           details={'operation': route.operation, 'reason': 'not an object'},
                           action=tr('сервисный слой вернул не объект',
                                     'the service layer did not return an object'))
        body = _guard_scope(principal, _redact(result, principal))
        if route.paginated:
            if not isinstance(body.get('items'), list) or not isinstance(body.get('stream_id'), str):
                raise ApiError('E_SERVICE_UNAVAILABLE', status=503,
                               details={'operation': route.operation,
                                        'reason': 'items and stream_id are required'},
                               action=tr('сервисный слой нарушил контракт постраничного ответа',
                                         'the service layer broke the paged response contract'))
            cursor_stream = (query or {}).get('cursor_stream')
            if cursor_stream and cursor_stream != body['stream_id']:
                raise field_error('cursor', tr('курсор другого потока', 'cursor of another stream'),
                                  code='E_CONFLICT_REVISION',
                                  action=tr('начните выдачу заново', 'restart the listing'))
            if body.get('cursor_seq') is not None and \
                    not isinstance(body['cursor_seq'], int):
                raise ApiError('E_SERVICE_UNAVAILABLE', status=503,
                               details={'operation': route.operation, 'reason': 'cursor_seq'},
                               action=tr('\u0441\u0435\u0440\u0432\u0438\u0441 \u043d\u0430\u0440\u0443\u0448\u0438\u043b \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u043f\u043e\u0441\u0442\u0440\u0430\u043d\u0438\u0446\u0430',
                                         'the service layer broke the paged response contract'))
            next_seq = body.get('next_seq')
            body['next_cursor'] = (encode_cursor(body['stream_id'], next_seq,
                                                 self.filter_digest(request.path, query or {}))
                                   if next_seq is not None else None)
        if route.async_job:
            if not isinstance(body.get('job_id'), str) or not body['job_id']:
                raise ApiError('E_SERVICE_UNAVAILABLE', status=503,
                               details={'operation': route.operation, 'reason': 'job_id is required'},
                               action=tr('долгая операция обязана вернуть job_id',
                                         'a long operation must return a job id'))
            headers.append(('Location', f'{API_PREFIX}/jobs/{body["job_id"]}'))
        payload = json.dumps(body, ensure_ascii=False, default=str).encode('utf-8')
        if route.method == 'GET':
            etag = '"' + hashlib.sha256(payload).hexdigest()[:32] + '"'
            headers.append(('ETag', etag))
            headers.extend(self._cors_headers(request))
            if (request.header('If-None-Match') or '').strip() == etag:
                return Response(304, b'', 'application/json; charset=utf-8', tuple(headers))
        return Response(202 if route.async_job else 200, payload,
                        'application/json; charset=utf-8', tuple(headers))

    def _stream(self, request, route, params, principal):
        query = parse_query_fields(route.query, request.query)
        stream_id = query.get('stream') or f"job:{params.get('id', 'system')}"
        seq = 0
        cursor = query.get('cursor')
        if cursor:
            cursor_stream, seq, _ = decode_cursor(cursor)
            if cursor_stream != stream_id:
                raise field_error('cursor', tr('курсор другого потока', 'cursor of another stream'),
                                  action=tr('начните подписку заново', 'restart the subscription'))
            query['cursor_stream'] = cursor_stream
            query['cursor_seq'] = seq
            if self.history.known(cursor_stream, seq) is False:
                raise ApiError('E_CONFLICT_REVISION', status=409,
                               details={'stream': cursor_stream, 'cursor_seq': seq,
                                        'max_event_history': self.config.max_event_history},
                               action=tr('события вытеснены ограниченной историей: перечитайте '
                                         'состояние ресурса',
                                         'events were evicted by the bounded history: '
                                         're-read the resource state'))
        call = Call(operation=route.operation, principal=principal, params=params,
                    query=query, path=request.path, method=request.method,
                    deadline_s=self.config.sse_reverify_s)
        try:
            source = self.service.invoke(route.operation, call)
        except ApiError:
            raise
        except Exception as exc:
            raise ApiError('E_SERVICE_UNAVAILABLE',
                           status=503 if isinstance(exc, NotImplementedError) else 500,
                           details={'reason': type(exc).__name__}) from exc
        # a service may return (stream_id, events) when it chose the stream itself
        declared, events = source if isinstance(source, tuple) else (stream_id, source)
        limit = int(query.get('limit') or self.config.max_limit)
        stream = EventStream(str(declared or stream_id),
                             self._guarded(events, limit, request), self.clock,
                             self.history, cursor_seq=seq)
        return Response(200, b'', 'text/event-stream; charset=utf-8',
                        (('Cache-Control', 'no-store'), ('X-Accel-Buffering', 'no'),
                         ('X-Workbench-Stream', stream.stream_id)), stream=stream)

    def _guarded(self, events, limit, request):
        """Bound the stream and re-check the key on a timer while it is open."""
        if not isinstance(events, Iterable):
            raise ApiError('E_SERVICE_UNAVAILABLE', status=503,
                           details={'reason': 'events are not iterable'})
        last = 0
        checked_at = self.clock()
        for index, event in enumerate(events):
            if index >= limit:
                yield {'seq': last + 1, 'type': 'stream.closed', 'code': 'E_LIMIT_BUDGET',
                       'data': {'reason': 'limit reached'}}
                return
            now = self.clock()
            if now - checked_at >= max(self.config.sse_reverify_s, 0.0):
                try:
                    self._recheck(request)
                except ApiError as exc:
                    yield {'seq': last + 1, 'type': 'session.closed', 'code': exc.code,
                           'data': {'reason': exc.message}}
                    return
                checked_at = now
            if isinstance(event, dict):
                last = max(last, int(event.get('seq') or 0))
            yield event

    def _recheck(self, request):
        """A key that expired or was revoked closes the open stream, per policy."""
        secret = self._presented_secret(request, None)
        if not secret:
            raise ApiError('E_AUTH_MISSING')
        principal = self.keys.verify(secret)
        if principal is None:
            raise ApiError('E_AUTH_INVALID', status=401)
        principal.check_time(self.clock())

    def _presented_secret(self, request, route):
        """The secret of a request: a header, or the deprecated query token of a read."""
        header = request.header('Authorization') or ''
        if header:
            scheme, _, value = header.partition(' ')
            if scheme.lower() == 'bearer' and value.strip():
                return value.strip()
        if route is None or (route.legacy_token and self.config.allow_query_token):
            return (parse_qs(request.query).get('token') or [''])[-1]
        return ''

    def _audit_denial(self, request, exc):
        """A refused request is worth a record: who asked, what, and why it failed."""
        method = 'GET' if request.method == 'HEAD' else request.method
        route, params = match_route(method, request.path)
        if route is None:
            return
        principal = None
        secret = self._presented_secret(request, route)
        if secret:
            try:
                principal = self.keys.verify(secret)
            except Exception:
                principal = None
        self._audit(route, request, params, principal, 'denied', exc.code, {})

    def _audit(self, route, request, params, principal, result, error_code, body):
        if not route.mutating and error_code is None:
            return
        collection_id = body.get('collection_id')
        pool_id = body.get('pool_id')
        for kind, source in route.scope:
            if source in params:
                if kind == 'collection':
                    collection_id = params[source]
                else:
                    pool_id = params[source]
        record = {'at': self.clock(), 'key_id': principal.key_id if principal else None,
                  'operation': route.operation, 'object_kind': route.tags[0] if route.tags else None,
                  'object_id': params.get('id') or params.get('endpoint_id') or body.get('name'),
                  'collection_id': collection_id, 'pool_id': pool_id,
                  'result': result, 'error_code': error_code}
        try:
            self.keys.audit(record)
        except Exception:  # an audit failure must not fail the request
            pass

    def _error(self, request, exc):
        headers = []
        if exc.retry_after is not None:
            headers.append(('Retry-After', str(int(exc.retry_after))))
        if exc.allow:
            headers.append(('Allow', exc.allow))
        headers.extend(self._cors_headers(request))
        payload = json.dumps(exc.body(), ensure_ascii=False).encode('utf-8')
        return Response(exc.status, payload, 'application/json; charset=utf-8', tuple(headers))


# --------------------------------------------------------------------------
# OpenAPI artifact
# --------------------------------------------------------------------------

def openapi_document():
    """The single description of the routes; openapi.json is its snapshot."""
    paths = {}
    for route in ROUTES:
        paths.setdefault(route.path, {})[route.method.lower()] = route.spec()
    return {
        'openapi': '3.1.0',
        'info': {
            'title': f'{PRODUCT_NAME} control API',
            'version': API_VERSION,
            'summary': 'Versioned control API with key management, jobs and events.',
            'description': (
                'Every route lives under /v1. Keys travel in the Authorization header; '
                'a query token is a deprecated read-only compatibility path. Long operations '
                'answer 202 with a job id and never hold the request. Mutations require an '
                'Idempotency-Key. Errors use stable E_* codes with a recovery action. '
                'The default bind is loopback; a remote bind needs keys and TLS or a '
                'reverse proxy, and a Bearer token is not encryption.'),
            'license': {'name': 'MIT', 'identifier': 'MIT'},
        },
        'servers': [{'url': f'http://127.0.0.1:{DEFAULT_PORT}',
                     'description': 'loopback default; a remote bind needs keys and TLS '
                                    'or a supported reverse proxy setup'}],
        'security': [{'bearerKey': []}],
        'tags': [{'name': name} for name in
                 ('service', 'keys', 'collections', 'sources', 'profiles', 'checks', 'jobs',
                  'results', 'pools', 'gateway', 'schedules', 'exports', 'reservations')],
        'paths': paths,
        'components': {
            'securitySchemes': {
                'bearerKey': {'type': 'http', 'scheme': 'bearer',
                              'description': 'api_key or subscription secret; '
                                             'never a query parameter'},
            },
            'schemas': {
                'Error': {'type': 'object', 'required': ['error'], 'properties': {'error': {
                    'type': 'object', 'required': ['code', 'message'],
                    'properties': {'code': {'type': 'string', 'enum': sorted(MESSAGES)},
                                   'message': {'type': 'string'},
                                   'action': {'type': 'string'},
                                   'details': {'type': 'object'}}}}},
            },
        },
        'x-permissions': sorted(PERMISSIONS),
        'x-error-codes': sorted(MESSAGES),
        'x-network-model': {
            'default_bind': 'loopback; every non-loopback bind must set allow_remote_bind and allowed_hosts',
            'remote_bind': 'a key is required on every route, including /v1/version',
            'tls': 'ApiConfig(tls_cert=..., tls_key=...) wraps the listener in TLS 1.2+, or run a '
                   'supported reverse proxy in front and set allowed_hosts to its name',
            'bearer_is_not_encryption': 'an Authorization header is not a secure channel; use TLS '
                                        'or a reverse proxy for anything that leaves this machine',
            'host_check': 'loopback binds accept a loopback Host only; other binds accept the names '
                          'in allowed_hosts',
            'origin_check': 'a request with an Origin is refused unless it is in allowed_origins; '
                            'CORS headers are echoed only for origins in cors_origins',
            'key_transport': 'Authorization: Bearer only; ?token= is a deprecated read-only path',
            'subscription_secret': 'read-only, no admin, private fields redacted, with expiry and revoke',
        },
        'x-limits': {
            'max_body_bytes': MAX_BODY_BYTES,
            'max_page_limit': MAX_PAGE_LIMIT,
            'default_rate_limit': 'requests per window, per identity and route; 429 with Retry-After',
            'request_timeout_s': 'deadline handed to the service layer in Call.deadline_s',
        },
    }


def openapi_path():
    """The packaged OpenAPI artifact, generated from :func:`openapi_document`."""
    return Path(__file__).with_name(OPENAPI_FILE)


def make_server(*, service, keys, host='127.0.0.1', port=DEFAULT_PORT, **options):
    """Build the HTTP server around an injected service layer and key store."""
    config = ApiConfig(host=host, port=port, **options)
    return ApiV1(service, keys, config).make_server()
