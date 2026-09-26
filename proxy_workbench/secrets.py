"""Own proxy credentials: a vault-backed secret store, access identities and rotation.

Implements F04. Contract references: CONTRACTS.ru.md §1.2(1) (an access is not an
endpoint, so two passwords of one address are two access identities), §3.3
migration 3 (the `accesses` table this module reads and writes, and only those
columns), §5.1 (`upstream_credential` is a fourth identity, separate from
`api_key`, `gui_session` and `gateway_password`) and §5.4 (the `E_SECRET_*` codes).

Three rules shape the whole module:

* the plaintext secret lives in the vault only. SQLite gets an opaque
  `secret_ref`, and no API row, log line, export, settings file, temporary file
  or argv ever receives the value;
* a credential change bumps `access_revision`, and an admission is only valid for
  the exact `(access_id, access_revision)` pair, so a new password cannot inherit
  a successful check of the old one;
* the vault and SQLite are not one transaction, so every write goes through
  stage -> commit -> finalize with compensation on the way out and a
  `reconcile()` pass for whatever a crash left behind.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import re
import secrets as _secrets
import time
from dataclasses import dataclass, field, replace
from urllib.parse import quote, unquote, urlsplit

# Access modes. NTLM, Kerberos, SOCKS4 password and HTTP Digest are out of scope
# by contract (MASTER-PROMPT F04) and are refused explicitly, never silently.
MODE_NONE = 'none'
MODE_HTTP_BASIC = 'http_basic'
MODE_SOCKS5 = 'socks5'
MODE_BY_SCHEME = {
    'http': MODE_HTTP_BASIC,
    'https': MODE_HTTP_BASIC,
    'socks5': MODE_SOCKS5,
    'socks5h': MODE_SOCKS5,
    'socks4': MODE_NONE,
}
SCHEME_MODES = {
    MODE_HTTP_BASIC: ('http', 'https'),
    MODE_SOCKS5: ('socks5', 'socks5h'),
}
REFUSED_SCHEMES = {
    'socks4': 'SOCKS4 has no username/password authentication',
}

E_VAULT_LOCKED = 'E_SECRET_VAULT_LOCKED'
E_NOT_PROVIDED = 'E_SECRET_NOT_PROVIDED'
E_AUTH_REQUIRED = 'E_SECRET_UPSTREAM_AUTH_REQUIRED'
E_AUTH_FAILED = 'E_SECRET_UPSTREAM_AUTH_FAILED'
# Not in the CONTRACTS §5.4 SECRET row yet; requested in the handoff.
E_VAULT_UNAVAILABLE = 'E_SECRET_VAULT_UNAVAILABLE'
E_VALIDATION = 'E_VALIDATION_FIELD'
E_CONFLICT = 'E_CONFLICT_REVISION'

VERIFIER_ALGO = 'pbkdf2_sha256'
# Starter value, not a claim of an optimum: a higher cost only makes a rotation
# slower, never loses a secret, so callers may raise it for their own hardware.
VERIFIER_ITERATIONS = 120_000
SALT_BYTES = 16
MAX_SECRET_BYTES = 4096
_LABEL_RE = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$')
REDACTED = '***'


class SecretError(Exception):
    """Base error. `code` is one of the E_* values from CONTRACTS §5.4."""

    code = E_NOT_PROVIDED

    def __init__(self, message, *, code=None, action=None, detail=None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        # Every useful error names the next step, not only its class (F25).
        self.action = action
        self.detail = detail

    def describe(self):
        # A detail often quotes a proxy URL, so it goes through the same redaction
        # as any other string that leaves this module.
        return {'code': self.code, 'message': redact_proxy_url(self.message),
                'action': self.action, 'detail': redact_proxy_url(self.detail)}


class SecretValidationError(SecretError):
    code = E_VALIDATION


class SecretUnsupportedError(SecretValidationError):
    """A requested auth mechanism is outside the agreed F04 scope."""


class SecretConflictError(SecretError):
    code = E_CONFLICT


class SecretVaultLocked(SecretError):
    code = E_VAULT_LOCKED


class SecretVaultUnavailable(SecretError):
    code = E_VAULT_UNAVAILABLE


class SecretNotProvided(SecretError):
    code = E_NOT_PROVIDED


class SecretAuthRequired(SecretError):
    code = E_AUTH_REQUIRED


class SecretAuthFailed(SecretError):
    code = E_AUTH_FAILED


# --------------------------------------------------------------------------- #
# One-way verifiers
# --------------------------------------------------------------------------- #

def make_verifier(secret, *, salt=None, iterations=VERIFIER_ITERATIONS):
    """Return `algo$iterations$salt$hash` for a secret. The secret is not recoverable.

    The salt is random per call unless the caller supplies one, so two accesses
    with the same password do not produce the same stored string.
    """
    raw = _secret_bytes(secret)
    salt_bytes = _secrets.token_bytes(SALT_BYTES) if salt is None else salt
    if not isinstance(salt_bytes, (bytes, bytearray)) or not 8 <= len(salt_bytes) <= 64:
        raise SecretValidationError('verifier salt must be 8..64 raw bytes')
    cost = int(iterations)
    if cost < 1:
        raise SecretValidationError('verifier iterations must be >= 1')
    digest = hashlib.pbkdf2_hmac('sha256', raw, bytes(salt_bytes), cost)
    return '%s$%d$%s$%s' % (VERIFIER_ALGO, cost,
                            base64.b64encode(bytes(salt_bytes)).decode('ascii'),
                            base64.b64encode(digest).decode('ascii'))


def verify_secret(candidate, verifier):
    """Constant-time comparison against a stored verifier.

    A missing, foreign or malformed verifier is False, never an exception, so a
    caller cannot tell the cases apart by what it can catch.

    The stored text has to be the exact string :func:`make_verifier` wrote.  A
    base64 field has spare bits in its last character, so two different strings
    can decode to the same digest; accepting a non-canonical spelling would let
    a rewritten or truncated row authenticate as if it were untouched, and it
    would make the pair a one-way function in only one direction.  Re-encoding
    and comparing the text is constant work on a fixed-length field and happens
    before the (much more expensive) key derivation, so the check costs nothing
    on a matching secret either.
    """
    parts = verifier.split('$') if isinstance(verifier, str) else ()
    if len(parts) != 4 or parts[0] != VERIFIER_ALGO:
        return False
    try:
        cost = int(parts[1])
        salt = _b64_exact(parts[2])
        expected = _b64_exact(parts[3])
    except (ValueError, binascii.Error):
        return False
    if cost < 1 or not 8 <= len(salt) <= 64 or not expected:
        return False
    try:
        digest = hashlib.pbkdf2_hmac('sha256', _secret_bytes(candidate), salt, cost)
    except SecretError:
        return False
    return hmac.compare_digest(digest, expected)


def _b64_exact(text):
    """Decode base64 and refuse any spelling that is not the canonical one."""
    raw = base64.b64decode(text, validate=True)
    if base64.b64encode(raw).decode('ascii') != text:
        raise binascii.Error('non-canonical base64 in a stored verifier')
    return raw


def _secret_bytes(secret):
    if isinstance(secret, (bytes, bytearray)):
        raw = bytes(secret)
    elif isinstance(secret, str):
        raw = secret.encode('utf-8')
    else:
        raise SecretValidationError('secret must be str or bytes',
                                    action='pass the password as text or as bytes')
    if not raw:
        # A blank password is the shape of a half-filled form, and the verifier
        # cannot be built from nothing, so the message names the other answer.
        raise SecretValidationError('secret must not be empty',
                                    action='enter the password, or create the access without one')
    if len(raw) > MAX_SECRET_BYTES:
        raise SecretValidationError('secret is longer than %d bytes' % MAX_SECRET_BYTES,
                                    action='shorten the password')
    return raw


# --------------------------------------------------------------------------- #
# Vaults
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SecretPayload:
    """What one access needs in order to authenticate.

    `previous_verifier` belongs to the revision that was current before the last
    rotation. It is what lets a reader holding an old revision fail loudly
    instead of quietly using the new password.
    """
    username: str
    password: str
    verifier: str
    revision: int = 1
    previous_verifier: str | None = None

    def matches(self, candidate):
        return verify_secret(candidate, self.verifier)

    def matches_previous(self, candidate):
        return bool(self.previous_verifier) and verify_secret(candidate, self.previous_verifier)


class Vault:
    """Storage for secrets. Implementations must not expose a file path."""

    name = 'abstract'
    is_persistent = False
    storage_path = None

    def stage(self, ref, payload):  # pragma: no cover - interface
        raise NotImplementedError

    def mark_ready(self, ref):  # pragma: no cover - interface
        raise NotImplementedError

    def get(self, ref):  # pragma: no cover - interface
        raise NotImplementedError

    def state(self, ref):  # pragma: no cover - interface
        raise NotImplementedError

    def delete(self, ref):  # pragma: no cover - interface
        raise NotImplementedError

    def refs(self):  # pragma: no cover - interface
        raise NotImplementedError

    @property
    def locked(self):
        return False

    def lock(self):
        """No-op for a vault without a lock state."""

    def unlock(self):
        """No-op for a vault without a lock state."""

    def account(self, ref):
        """The keyring account name for a reference. The ref carries no secret."""
        return 'access:%s' % ref


class MemoryVault(Vault):
    """In-memory vault. Holds nothing on disk and survives no process exit."""

    name = 'memory'

    def __init__(self, *, now=None):
        self._entries = {}
        self._locked = False
        self._now = now or time.time

    @property
    def locked(self):
        return self._locked

    def lock(self):
        self._locked = True

    def unlock(self):
        self._locked = False

    def _check(self, ref=None):
        if self._locked:
            raise SecretVaultLocked(
                'secret store is locked',
                action='unlock the secret store and repeat the request')

    # -- vault protocol ----------------------------------------------------- #
    def stage(self, ref, payload):
        self._check(ref)
        if not isinstance(payload, SecretPayload):
            raise SecretValidationError('payload must be a SecretPayload')
        self._entries[ref] = {'state': 'staged', 'payload': payload}
        return ref

    def mark_ready(self, ref):
        self._check(ref)
        entry = self._entries.get(ref)
        if entry is None:
            raise SecretNotProvided('unknown secret reference',
                                    action='recreate the access; the reference is not in the store')
        entry['state'] = 'ready'
        return ref

    def get(self, ref):
        self._check(ref)
        entry = self._entries.get(ref)
        if entry is None:
            raise SecretNotProvided('secret reference is not in the store',
                                    action='re-enter the credential for this access')
        return entry['payload']

    def state(self, ref):
        self._check(ref)
        entry = self._entries.get(ref)
        return None if entry is None else entry['state']

    def delete(self, ref):
        self._check(ref)
        return self._entries.pop(ref, None) is not None

    def refs(self):
        self._check()
        return sorted(self._entries)

    def close(self):
        """Wipe every entry. Used when a session ends."""
        self._entries.clear()


_SESSIONS = {}


class SessionVault(MemoryVault):
    """Session-only adapter: the secret lives in one process and dies with it.

    A separate process that knows only the reference gets `SecretVaultLocked`,
    which is the documented behaviour for session refs (roadmap R1 item 4): a
    detached consumer asks for new input instead of inheriting a secret.
    """

    name = 'session'

    def __init__(self, session_id=None, *, lifetime_s=None, now=None):
        super().__init__(now=now)
        self.session_id = session_id or 'session-%s' % _secrets.token_hex(8)
        self.lifetime_s = lifetime_s
        self._born_at = self._now()
        _SESSIONS[self.session_id] = self

    @classmethod
    def claim(cls, session_id):
        """Return the vault of this process, or raise. Never opens a new session."""
        vault = _SESSIONS.get(session_id)
        if vault is None:
            raise SecretVaultLocked(
                'session secret %s is not available in this process' % session_id,
                action='start the session in this process or enter the credential again')
        return vault

    @classmethod
    def forget(cls, session_id):
        return _SESSIONS.pop(session_id, None) is not None

    def close(self):
        super().close()
        _SESSIONS.pop(self.session_id, None)

    @property
    def expired(self):
        if self.lifetime_s is None:
            return False
        return (self._now() - self._born_at) >= self.lifetime_s

    def _check(self, ref=None):
        super()._check(ref)
        if self.expired:
            raise SecretNotProvided(
                'session secret expired after %s seconds' % self.lifetime_s,
                action='enter the credential again; session secrets are not stored on disk')


def _import_keyring():
    try:
        import keyring
    except ImportError:
        return None
    return keyring


class OsVault(Vault):
    """OS keychain vault through `keyring`, when that optional dependency exists.

    Without it `OsVault.available()` is False and `open_vault()` falls back to the
    session adapter, never to a plaintext file. The macOS `security` CLI is
    deliberately not used: it only accepts a password as an argument, which is
    exactly the argv leak F04 forbids.
    """

    name = 'os-vault'
    is_persistent = True

    def __init__(self, service='proxy-workbench', *, keyring_module=None):
        self.service = service
        self._keyring = keyring_module if keyring_module is not None else _import_keyring()
        if self._keyring is None:
            raise SecretVaultUnavailable(
                'no OS secret store is available in this installation',
                action='install the optional "keyring" dependency or use a session secret')

    @staticmethod
    def available():
        return _import_keyring() is not None

    def stage(self, ref, payload):
        if not isinstance(payload, SecretPayload):
            raise SecretValidationError('payload must be a SecretPayload')
        self._keyring.set_password(self.service, self.account(ref), json.dumps({
            'username': payload.username,
            'password': payload.password,
            'verifier': payload.verifier,
            'revision': payload.revision,
            'previous_verifier': payload.previous_verifier,
        }, ensure_ascii=False))
        return ref

    def mark_ready(self, ref):
        # A keychain entry has no staged state; writing it is the commit.
        self.get(ref)
        return ref

    def get(self, ref):
        try:
            raw = self._keyring.get_password(self.service, self.account(ref))
        except Exception as exc:  # keyring raises backend-specific errors
            raise SecretVaultLocked('secret store refused the read: %s' % type(exc).__name__,
                                    action='unlock the OS secret store and repeat') from exc
        if raw is None:
            raise SecretNotProvided('secret reference is not in the store',
                                    action='re-enter the credential for this access')
        try:
            data = json.loads(raw)
            return SecretPayload(username=data['username'], password=data['password'],
                                 verifier=data['verifier'], revision=int(data['revision']),
                                 previous_verifier=data.get('previous_verifier'))
        except (ValueError, KeyError, TypeError) as exc:
            raise SecretNotProvided('stored secret entry is unreadable',
                                    action='re-enter the credential for this access') from exc

    def state(self, ref):
        self.get(ref)
        return 'ready'

    def delete(self, ref):
        try:
            self._keyring.delete_password(self.service, self.account(ref))
        except Exception:
            return False
        return True

    def refs(self):
        # A keychain cannot enumerate our own entries portably, so reconciliation
        # over an OS vault is driven by SQLite instead of by scanning the vault.
        return []


def open_vault(preference='auto', *, service='proxy-workbench', session_id=None, lifetime_s=None):
    """Return the agreed secret store: OS vault when possible, else session-only.

    `preference='os'` refuses to fall back, so a caller that promised a
    persistent store learns that it is unavailable instead of silently
    downgrading to a value that dies with the process.
    """
    if preference not in ('auto', 'os', 'session'):
        raise SecretValidationError('vault preference must be auto, os or session')
    if preference in ('auto', 'os') and OsVault.available():
        return OsVault(service)
    if preference == 'os':
        raise SecretVaultUnavailable(
            'no OS secret store is available in this installation',
            action='install the optional "keyring" dependency or use a session secret')
    return SessionVault(session_id, lifetime_s=lifetime_s)


def new_reference():
    """An opaque vault reference. Random, so it discloses nothing about a secret."""
    return 'sec_%s' % _secrets.token_hex(16)


# --------------------------------------------------------------------------- #
# Endpoints on the access path
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Endpoint:
    """A proxy address without credentials. One access belongs to one of these."""

    scheme: str
    host: str
    port: int
    ip_version: int | None = None

    @property
    def is_hostname(self):
        return self.ip_version is None

    @property
    def authority(self):
        return '[%s]:%d' % (self.host, self.port) if self.ip_version == 6 else '%s:%d' % (self.host, self.port)

    @property
    def canonical(self):
        return '%s://%s' % (self.scheme, self.authority)

    def url(self, resolved=None):
        """Canonical URL, with credentials only when a caller explicitly asks.

        F04 and CONTRACTS §4.4: an exported or published row must never carry
        userinfo, so the plain form is what every default caller gets. The
        userinfo form exists for a transport that needs it (httpx takes proxy
        credentials in the URL) and for an explicitly authorized secret export.
        """
        if resolved is None:
            return self.canonical
        return '%s://%s@%s' % (self.scheme,
                               quote(resolved.username, safe='') + ':' + quote(resolved.password, safe=''),
                               self.authority)


def split_userinfo(value):
    """Split `scheme://user:pass@host:port` into (bare url, username, password).

    An import line carries the credential; the collection normalizer must never
    see it, so the userinfo is peeled off first and turned into a vault entry.
    Returns percent-decoded values, or None when the input is not a proxy URL:
    no port, no path, query or fragment, no whitespace.
    """
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or any(char.isspace() for char in raw):
        return None
    if '://' not in raw:
        raw = 'http://' + raw
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if not parsed.scheme or not parsed.hostname or port is None:
        return None
    if parsed.path not in ('', '/') or parsed.query or parsed.fragment:
        return None
    username = unquote(parsed.username) if parsed.username is not None else None
    password = unquote(parsed.password) if parsed.password is not None else None
    # hostname loses the IPv6 brackets, so put them back before rebuilding.
    host = '[%s]' % parsed.hostname if ':' in parsed.hostname else parsed.hostname
    bare = '%s://%s:%d' % (parsed.scheme, host, port)
    return bare, username, password


def parse_endpoint(value, *, normalizer=None):
    """Parse a user proxy into an `Endpoint`, refusing userinfo and paths.

    `normalizer` is the caller's shared canonicalizer (proxytool.normalize_custom
    today). When it is given, the bare `scheme://host:port` string is handed to
    it instead of being canonicalized here, so the access path cannot become a
    second normalizer. Without it this module falls back to its own strict
    canonicalization, which additionally IDNA-encodes hostnames.
    """
    split = split_userinfo(value)
    if split is None:
        raise SecretValidationError('not a proxy address: %r' % _excerpt(value),
                                    action='use scheme://host:port')
    bare, username, password = split
    if username is not None:
        del password
        raise SecretValidationError(
            'credentials must not travel inside the address',
            action='pass the credential separately so it can go into the secret store')
    if normalizer is not None:
        canonical = normalizer(bare)
        if not canonical:
            raise SecretValidationError('address rejected by the shared normalizer: %r' % _excerpt(bare),
                                        action='check the scheme, host and port')
        return _from_canonical(canonical)
    try:
        parsed = urlsplit(bare)
        host = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        raise SecretValidationError('address is not a URL: %r' % _excerpt(bare)) from None
    if not host:
        raise SecretValidationError('address has no host')
    if port is None or not 1 <= port <= 65535:
        raise SecretValidationError('port must be 1..65535, got %r' % (port,))
    scheme = parsed.scheme.lower()
    if scheme not in MODE_BY_SCHEME:
        raise SecretUnsupportedError('unsupported proxy scheme %r' % scheme,
                                     action='supported schemes: %s' % ', '.join(sorted(MODE_BY_SCHEME)))
    canonical_host, version = _canonicalize_host(host)
    return Endpoint(scheme=scheme, host=canonical_host, port=port, ip_version=version)


def _from_canonical(canonical):
    try:
        parsed = urlsplit(canonical)
        host = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        raise SecretValidationError('shared normalizer returned %r' % _excerpt(canonical)) from None
    if not host or port is None:
        raise SecretValidationError('shared normalizer returned %r' % _excerpt(canonical))
    version = None if _looks_like_hostname(host) else ipaddress.ip_address(host).version
    return Endpoint(scheme=parsed.scheme.lower(), host=host, port=port, ip_version=version)


def _canonicalize_host(host):
    """Return (canonical host, ip version or None). Handles IPv4, IPv6 and IDNA."""
    text = host.strip().rstrip('.').lower()
    if not text:
        raise SecretValidationError('address has an empty host')
    if not _looks_like_hostname(text):
        ip = ipaddress.ip_address(text)
        return ip.compressed, ip.version
    try:
        # IDNA first, so a non-ASCII name is validated in its encoded form.
        encoded = text.encode('idna').decode('ascii')
    except UnicodeError as exc:
        raise SecretValidationError('hostname cannot be encoded as IDNA: %s' % exc,
                                    action='use letters, digits, hyphens and dots') from None
    labels = encoded.split('.')
    if len(encoded) > 253 or any(not label or len(label) > 63 or not _LABEL_RE.match(label)
                                 for label in labels):
        raise SecretValidationError('hostname has an invalid label or is too long',
                                    action='use letters, digits, hyphens and dots; IDNA names are encoded')
    return encoded, None


def _looks_like_hostname(text):
    if '.' not in text and ':' not in text:
        return True
    try:
        ipaddress.ip_address(text.strip('[]'))
    except ValueError:
        return True
    return False


def auth_mode_for(scheme, *, username=None, password=None):
    """Map a scheme to an access mode, refusing mechanisms F04 excludes."""
    name = str(scheme or '').lower()
    if name not in MODE_BY_SCHEME:
        raise SecretUnsupportedError('unsupported proxy scheme %r' % name,
                                     action='supported schemes: %s' % ', '.join(sorted(MODE_BY_SCHEME)))
    if name in REFUSED_SCHEMES and (username or password):
        raise SecretUnsupportedError(REFUSED_SCHEMES[name],
                                     action='use HTTP or SOCKS5 credentials instead')
    if username is None and password is None:
        return MODE_NONE
    if username is None or password is None:
        raise SecretValidationError('a username and a password are both required',
                                    action='enter both, or neither for an open proxy')
    return MODE_BY_SCHEME[name]


def check_mode_scheme(mode, scheme):
    """Check an access mode against the scheme it will be used with."""
    if mode not in (MODE_NONE, MODE_HTTP_BASIC, MODE_SOCKS5):
        raise SecretUnsupportedError('unknown access mode %r' % mode,
                                     action='modes: %s' % ', '.join((MODE_NONE, MODE_HTTP_BASIC, MODE_SOCKS5)))
    allowed = SCHEME_MODES.get(mode)
    if allowed is not None and str(scheme or '').lower() not in allowed:
        raise SecretValidationError('access mode %r does not authenticate %s' % (mode, scheme),
                                    action='use a separate access identity for this endpoint')
    return mode


def requires_auth(scheme, state):
    """True when a scheme needs credentials that this access does not carry.

    A proxy that answers 407 or a SOCKS5 "username/password required" reply is
    this case, and it is a different outcome from a wrong password.
    """
    name = str(scheme or '').lower()
    if name in SCHEME_MODES.get(MODE_HTTP_BASIC, ()) or name in SCHEME_MODES.get(MODE_SOCKS5, ()):
        return state not in (STATE_READY, STATE_NO_REF)
    return False


def classify_upstream(status=None, *, state=None, credentials_sent=False, detail=None):
    """Turn an upstream outcome into one of the distinguishable F04 failures.

    Returns the error to raise, or None when the outcome is not a credential
    problem. The four cases stay apart on purpose (MASTER-PROMPT F04): a proxy
    that wants auth we did not send, a credential it rejected, a store we could
    not read, and a credential we never had.
    """
    if state == STATE_LOCKED:
        return SecretVaultLocked('the secret store is locked',
                                 action='unlock the secret store and repeat the request', detail=detail)
    if status == 407:
        error = SecretAuthFailed if credentials_sent else SecretAuthRequired
        return error('the proxy demands credentials' if credentials_sent
                     else 'the proxy requires authentication',
                     detail=detail, action='store a credential for this access and check it again')
    if state in (STATE_STAGED, STATE_MISSING):
        return SecretNotProvided('no usable credential for this access (%s)' % state,
                                 action='re-enter the credential for this access', detail=detail)
    return None


# --------------------------------------------------------------------------- #
# Access records (CONTRACTS §3.3, migration 3)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Access:
    """One credential identity for one endpoint. Mirrors the `accesses` columns."""

    id: str
    endpoint_id: str
    mode: str
    secret_ref: str | None
    access_revision: int
    created_at: float
    rotated_at: float | None = None

    @classmethod
    def from_row(cls, row):
        return cls(id=row['id'], endpoint_id=row['endpoint_id'], mode=row['mode'],
                   secret_ref=row['secret_ref'], access_revision=row['access_revision'],
                   created_at=row['created_at'], rotated_at=row['rotated_at'])

    def as_row(self):
        return (self.id, self.endpoint_id, self.mode, self.secret_ref,
                self.access_revision, self.created_at, self.rotated_at)


COLUMNS = 'id, endpoint_id, mode, secret_ref, access_revision, created_at, rotated_at'
SELECT = 'SELECT %s FROM accesses' % COLUMNS

STATE_READY = 'ready'
STATE_STAGED = 'staged'
STATE_MISSING = 'missing'
STATE_LOCKED = 'locked'
STATE_NO_REF = 'no_ref'


@dataclass
class ResolvedAccess:
    """A secret in hand. Scrub it, or use it as a context manager, when done."""

    access: Access
    mode: str
    username: str
    password: str
    _scrubbed: bool = field(default=False, compare=False, repr=False)

    @property
    def auth(self):
        return (self.username, self.password)

    def scrub(self):
        """Drop this object's reference to the password. The vault keeps its copy.

        CPython strings cannot be zeroed, which is the reason the secret lives
        in the vault instead of in a settings file, and why the resolved value
        is meant to be short-lived.
        """
        self.password = ''
        self._scrubbed = True
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.scrub()
        return False

    def describe(self):
        return describe(self.access)


# --------------------------------------------------------------------------- #
# Transport credentials (F04: HTTP Basic and SOCKS5 username/password)
# --------------------------------------------------------------------------- #

#: RFC 1929 sub-negotiation version, and the length that bounds each of its two
#: fields.  A longer field is a protocol error upstream, so it is refused here,
#: where the message can still name the cause.
SOCKS5_AUTH_VERSION = 1
SOCKS5_FIELD_MAX = 255
#: The two authentication methods F04 supports.  An access offers exactly one.
SOCKS5_METHOD_NO_AUTH = 0x00
SOCKS5_METHOD_USER_PASSWORD = 0x02


def proxy_authorization(username, password):
    """`Proxy-Authorization: Basic ...` for an HTTP or HTTPS proxy (RFC 7617).

    An empty result means "send no header": an open proxy is the common case and
    a blank credential in a header is a different thing from no header at all.
    """
    if not username and not password:
        return ''
    if not username or password is None:
        raise SecretValidationError('a username and a password are both required',
                                    action='enter both, or neither for an open proxy')
    token = base64.b64encode(('%s:%s' % (username, password)).encode('utf-8')).decode('ascii')
    return 'Basic %s' % token


def socks5_greeting(*, with_auth):
    """The method offer of one SOCKS5 handshake.

    An access without a credential offers only "no authentication required" (0x00)
    and an access with one offers only "username/password" (0x02).  Offering both
    would let a proxy pick the weaker one, so the offer is derived from the access
    and never widened.
    """
    method = SOCKS5_METHOD_USER_PASSWORD if with_auth else SOCKS5_METHOD_NO_AUTH
    return bytes((0x05, 0x01, method))


def socks5_username_password(username, password):
    """The RFC 1929 username/password sub-negotiation for a SOCKS5 proxy."""
    if not username or password is None:
        raise SecretValidationError('a username and a password are both required',
                                    action='enter both, or neither for an open proxy')
    user = username.encode('utf-8')
    secret = password.encode('utf-8')
    if not 1 <= len(user) <= SOCKS5_FIELD_MAX or not 1 <= len(secret) <= SOCKS5_FIELD_MAX:
        raise SecretValidationError(
            'a SOCKS5 username and password must each be 1..%d bytes' % SOCKS5_FIELD_MAX,
            action='shorten the credential to what the SOCKS5 sub-negotiation can carry')
    return (bytes((SOCKS5_AUTH_VERSION, len(user))) + user
            + bytes((len(secret),)) + secret)


@dataclass(frozen=True)
class TransportCredentials:
    """What one transport needs to authenticate an upstream proxy.

    Held as bytes and a header value rather than as a live connection, so the
    socket stays the caller's business and this module never holds a transport.
    The value is short-lived by construction: it is built from a
    :class:`ResolvedAccess` the caller is already scrubbing.
    """

    mode: str
    scheme: str
    access_id: str
    access_revision: int
    proxy_authorization: str = ''
    socks5_greeting: bytes = b''
    socks5_auth: bytes = b''

    @property
    def authenticated(self):
        return self.mode != MODE_NONE

    def as_json(self):
        """A describable form. The credentials are counted, never carried."""
        return {'access_id': self.access_id, 'access_revision': self.access_revision,
                'mode': self.mode, 'scheme': self.scheme,
                'authenticated': self.authenticated}


def transport_credentials(access, resolved, *, scheme=None):
    """Build the wire material for one access, or refuse.

    This is the only sanctioned way to turn a resolved access into something a
    socket can send, which is what keeps the password on the short-lived value
    instead of in a URL, a query, a settings file or a log line.  The scheme is
    checked against the access mode, so a SOCKS5 identity can never be offered
    to an HTTP proxy and the other way round.
    """
    selected = str(scheme or '').lower()
    if selected not in MODE_BY_SCHEME:
        raise SecretUnsupportedError('unsupported proxy scheme %r' % selected,
                                     action='supported schemes: %s' % ', '.join(sorted(MODE_BY_SCHEME)))
    if resolved.access.id != access.id:
        raise SecretConflictError(
            'the resolved secret belongs to access %s, not %s'
            % (_excerpt(resolved.access.id), _excerpt(access.id)),
            action='resolve the access again and use the value it returns')
    if int(resolved.access.access_revision) != int(access.access_revision):
        raise SecretConflictError(
            'the resolved secret is revision %d but the access is at %d'
            % (resolved.access.access_revision, access.access_revision),
            action='re-resolve the access; a superseded secret is not sent')
    check_mode_scheme(resolved.mode, selected)
    if resolved.mode == MODE_NONE:
        return TransportCredentials(mode=MODE_NONE, scheme=selected, access_id=access.id,
                                    access_revision=access.access_revision,
                                    socks5_greeting=socks5_greeting(with_auth=False)
                                    if selected in SCHEME_MODES[MODE_SOCKS5] else b'')
    if selected in SCHEME_MODES[MODE_HTTP_BASIC]:
        return TransportCredentials(mode=resolved.mode, scheme=selected, access_id=access.id,
                                    access_revision=access.access_revision,
                                    proxy_authorization=proxy_authorization(resolved.username,
                                                                            resolved.password))
    if selected in SCHEME_MODES[MODE_SOCKS5]:
        return TransportCredentials(mode=resolved.mode, scheme=selected, access_id=access.id,
                                    access_revision=access.access_revision,
                                    socks5_greeting=socks5_greeting(with_auth=True),
                                    socks5_auth=socks5_username_password(resolved.username,
                                                                        resolved.password))
    raise SecretValidationError('access mode %r cannot authenticate %s' % (resolved.mode, selected),
                                action='use a separate access identity for this endpoint')


def admission_key(access, access_revision):
    """The pair an observation must carry to stay valid.

    CONTRACTS §1.2(1): a new `access_revision` is a different subject, so the old
    successful check cannot be read as evidence for the new credential.
    """
    return (access.id, int(access_revision))


def is_superseded(access, access_revision):
    """True when `access_revision` is an older revision of this same access."""
    return int(access_revision) < int(access.access_revision)


class AccessStore:
    """The staging protocol between a vault and SQLite, and the rotation rules.

    Every write is stage -> commit -> finalize. A failure in the middle is
    compensated here, and whatever could not be compensated is repaired by
    `reconcile()`, which is safe to run repeatedly.
    """

    def __init__(self, conn, vault, *, now=None, id_factory=None):
        self.conn = conn
        self.vault = vault
        self._now = now or time.time
        self._new_id = id_factory or (lambda: 'acc_%s' % _secrets.token_hex(12))

    # -- reads -------------------------------------------------------------- #
    def get(self, access_id):
        row = self.conn.execute('%s WHERE id = ?' % SELECT, (access_id,)).fetchone()
        return None if row is None else Access.from_row(row)

    def require(self, access_id):
        access = self.get(access_id)
        if access is None:
            raise SecretValidationError('unknown access %r' % _excerpt(access_id),
                                        action='re-import the endpoint and create the access again')
        return access

    def list_for_endpoint(self, endpoint_id):
        rows = self.conn.execute(
            '%s WHERE endpoint_id = ? ORDER BY created_at, id' % SELECT, (endpoint_id,)).fetchall()
        return [Access.from_row(row) for row in rows]

    def state(self, access):
        """Where an access stands relative to the vault."""
        if access.secret_ref is None:
            return STATE_NO_REF
        if self.vault.locked:
            return STATE_LOCKED
        state = self.vault.state(access.secret_ref)
        return STATE_MISSING if state is None else state

    def usable(self, access):
        state = self.state(access)
        if state == STATE_READY:
            return True
        return state == STATE_NO_REF and access.mode == MODE_NONE

    def find_by_username(self, endpoint_id, username):
        """Locate an existing access by the username held in the vault.

        Used to offer a conflict choice (rotate this access, or create a second
        one) instead of silently overwriting or silently duplicating. The
        username is a label rather than a secret, so an ordinary comparison is
        enough here; the password is never compared this way.
        """
        if username is None:
            return None
        for access in self.list_for_endpoint(endpoint_id):
            if access.secret_ref is None or self.state(access) != STATE_READY:
                continue
            if self.vault.get(access.secret_ref).username == username:
                return access
        return None

    # -- writes ------------------------------------------------------------- #
    def create(self, endpoint_id, scheme, *, username=None, password=None, mode=None, access_id=None):
        """Create a new access identity for an endpoint.

        Accesses are additive: two credentials of one endpoint are two `Access`
        rows, never one row overwritten.
        """
        if not isinstance(endpoint_id, str) or not endpoint_id.strip():
            raise SecretValidationError('endpoint_id is required')
        selected = mode or auth_mode_for(scheme, username=username, password=password)
        check_mode_scheme(selected, scheme)
        now = self._now()
        access = Access(id=access_id or self._new_id(), endpoint_id=endpoint_id, mode=selected,
                        secret_ref=None, access_revision=1, created_at=now, rotated_at=None)
        if selected == MODE_NONE:
            return self._insert(access)
        payload = SecretPayload(username=username, password=password,
                                verifier=make_verifier(password), revision=1)
        return self._insert(access, payload=payload)

    def _insert(self, access, *, payload=None):
        ref = new_reference() if payload is not None else None
        if ref is not None:
            self.vault.stage(ref, payload)
        stored = replace(access, secret_ref=ref)
        try:
            self.conn.execute(
                'INSERT INTO accesses (%s) VALUES (?,?,?,?,?,?,?)' % COLUMNS, stored.as_row())
            self.conn.commit()
        except Exception:
            # The vault is the only other writer here: drop the staged entry so a
            # failed create leaves no reference that nobody owns.
            if ref is not None:
                try:
                    self.vault.delete(ref)
                except SecretError:
                    pass
            raise
        if ref is None:
            return stored
        try:
            self.vault.mark_ready(ref)
        except SecretError as exc:
            exc.detail = {'compensated': False, 'access_id': stored.id,
                          'next': 'run reconcile() to finish or drop this access'}
            raise
        return stored

    def rotate(self, access_id, *, password=None, username=None, expect_revision=None):
        """Replace the credential of an access and bump `access_revision`.

        The reference stays the same (roadmap R1 acceptance: rotation of the same
        ref), so a superseded revision is recognisable only by its number, which
        is exactly why a new password cannot inherit the old evidence.
        """
        access = self.require(access_id)
        if expect_revision is not None and int(expect_revision) != access.access_revision:
            raise SecretConflictError(
                'access %s is at revision %d, not %d'
                % (_excerpt(access_id), access.access_revision, expect_revision),
                action='re-read the access and repeat the rotation if it is still the one you mean')
        if access.secret_ref is None:
            if password is None and username is None:
                return access
            raise SecretValidationError('access %s has no stored credential' % _excerpt(access_id),
                                        action='create a credentialed access instead')
        current = self.vault.get(access.secret_ref)
        if current.revision != access.access_revision:
            raise SecretConflictError(
                'secret store holds revision %d but the database says %d'
                % (current.revision, access.access_revision),
                action='run reconcile() before rotating again')
        new_password = current.password if password is None else password
        new_username = current.username if username is None else username
        if not new_username:
            raise SecretValidationError('a username is required', action='enter a username')
        revision = access.access_revision + 1
        staged = SecretPayload(username=new_username, password=new_password,
                               verifier=make_verifier(new_password), revision=revision,
                               previous_verifier=current.verifier)
        now = self._now()
        self.vault.stage(access.secret_ref, staged)
        try:
            changed = self.conn.execute(
                'UPDATE accesses SET access_revision = ?, rotated_at = ? '
                'WHERE id = ? AND access_revision = ?',
                (revision, now, access.id, access.access_revision)).rowcount
            self.conn.commit()
        except Exception:
            # Put the previous credential back before giving up, so a failed
            # rotation cannot leave a new password attached to an old revision.
            self._restore(access.secret_ref, current)
            raise
        if not changed:
            self._restore(access.secret_ref, current)
            raise SecretConflictError(
                'access %s was rotated by someone else' % _excerpt(access_id),
                action='re-read the access and repeat the rotation')
        try:
            self.vault.mark_ready(access.secret_ref)
        except SecretError as exc:
            exc.detail = {'access_id': access.id, 'access_revision': revision,
                          'next': 'run reconcile() to finalize this access'}
            raise
        return replace(access, access_revision=revision, rotated_at=now)

    def _restore(self, ref, payload):
        try:
            self.vault.stage(ref, payload)
            self.vault.mark_ready(ref)
        except SecretError:
            pass

    def purge(self, access_id):
        """Remove an access and its secret. Also the compensation for a create."""
        access = self.get(access_id)
        if access is None:
            return False
        self.conn.execute('DELETE FROM accesses WHERE id = ?', (access.id,))
        self.conn.commit()
        if access.secret_ref is not None and not self.vault.locked:
            try:
                self.vault.delete(access.secret_ref)
            except SecretError:
                pass
        return True

    # -- resolution --------------------------------------------------------- #
    def resolve(self, access_id, *, access_revision=None):
        """Return the secret for an access, refusing a superseded revision."""
        access = self.require(access_id)
        if access_revision is not None and int(access_revision) != access.access_revision:
            raise SecretConflictError(
                'access %s is at revision %d, revision %s was superseded'
                % (_excerpt(access_id), access.access_revision, access_revision),
                action='check the endpoint again with the current credential')
        state = self.state(access)
        if state == STATE_LOCKED:
            raise SecretVaultLocked('secret store is locked for access %s' % _excerpt(access_id),
                                    action='unlock the secret store and repeat the request')
        if not self.usable(access):
            raise SecretNotProvided(
                'secret for access %s is %s' % (_excerpt(access_id), state),
                action='re-enter the credential for this access')
        if access.secret_ref is None:
            return ResolvedAccess(access=access, mode=access.mode, username='', password='')
        payload = self.vault.get(access.secret_ref)
        if payload.revision != access.access_revision:
            raise SecretConflictError(
                'secret store holds revision %d but the database says %d'
                % (payload.revision, access.access_revision),
                action='run reconcile() before using this access')
        return ResolvedAccess(access=access, mode=access.mode,
                              username=payload.username, password=payload.password)

    def verify_password(self, access_id, candidate, *, access_revision=None):
        """Check a candidate password against the stored verifier, timing-safe."""
        access = self.require(access_id)
        if access.secret_ref is None:
            return False
        payload = self.vault.get(access.secret_ref)
        if access_revision is None or int(access_revision) == access.access_revision:
            return payload.matches(candidate)
        return payload.matches(candidate) or payload.matches_previous(candidate)

    # -- reconciliation ----------------------------------------------------- #
    def reconcile(self, *, now=None):
        """Repair what a crash between vault and SQLite left behind.

        Idempotent: references in the vault that no access row owns are removed,
        rows whose reference is still staged are finalized, and rows whose
        reference vanished are reported rather than invented.
        """
        moment = self._now() if now is None else now
        orphans, finalized, missing, locked = [], [], [], []
        rows = self.conn.execute(SELECT).fetchall()
        if self.vault.locked:
            locked = [row['id'] for row in rows if row['secret_ref']]
            return ReconciliationReport(locked_refs=tuple(locked), rows=len(rows), at=moment)
        referenced = {row['secret_ref'] for row in rows if row['secret_ref']}
        for ref in self.vault.refs():
            if ref not in referenced:
                self.vault.delete(ref)
                orphans.append(ref)
        for row in rows:
            access = Access.from_row(row)
            if access.secret_ref is None:
                continue
            state = self.vault.state(access.secret_ref)
            if state is None:
                missing.append(access.id)
            elif state != STATE_READY:
                payload = self.vault.get(access.secret_ref)
                if payload.revision != access.access_revision:
                    missing.append(access.id)
                    continue
                self.vault.mark_ready(access.secret_ref)
                finalized.append(access.id)
        return ReconciliationReport(removed_orphan_refs=tuple(orphans), finalized_refs=tuple(finalized),
                                    missing_refs=tuple(missing), rows=len(rows), at=moment)


@dataclass(frozen=True)
class ReconciliationReport:
    removed_orphan_refs: tuple = ()
    finalized_refs: tuple = ()
    missing_refs: tuple = ()
    locked_refs: tuple = ()
    rows: int = 0
    at: float = 0.0

    @property
    def clean(self):
        return not (self.removed_orphan_refs or self.finalized_refs
                    or self.missing_refs or self.locked_refs)

    def as_dict(self):
        return {
            'at': self.at,
            'rows': self.rows,
            'clean': self.clean,
            'removed_orphan_refs': list(self.removed_orphan_refs),
            'finalized_refs': list(self.finalized_refs),
            'missing_refs': list(self.missing_refs),
            'locked_refs': list(self.locked_refs),
        }


# --------------------------------------------------------------------------- #
# Destination policy for a collection
# --------------------------------------------------------------------------- #

KIND_PUBLIC = 'public'
KIND_TRUSTED_PRIVATE = 'trusted_private'

REASON_ALLOWED = 'allowed'
REASON_NOT_PUBLIC = 'address is not globally routable'
REASON_PRIVATE_ALLOWED = 'private address allowed by the trusted collection'
REASON_HOSTNAME = 'hostname needs a trusted collection and an allowed host'
REASON_HOST_NOT_ALLOWED = 'host is not in the collection allowlist'
REASON_LOOPBACK = 'loopback needs an explicit allow_loopback in a trusted collection'
REASON_DNS_FALLBACK = 'hostname resolves to a disallowed address'


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = REASON_ALLOWED
    address: str | None = None

    def __bool__(self):
        return self.allowed

    def as_dict(self):
        return {'allowed': self.allowed, 'reason': self.reason, 'address': self.address}


@dataclass(frozen=True)
class DestinationPolicy:
    """Which addresses a collection may measure, and where it may send them.

    Public discovery stays limited to globally routable addresses. Trusted
    private endpoints are an explicit mode of the user's own collection: private
    networks are only reachable when the collection says so, and a hostname is
    only reachable when every address it resolves to passes.
    """

    kind: str = KIND_PUBLIC
    allow_private_networks: bool = False
    allow_hostnames: bool = False
    allow_loopback: bool = False
    allow_link_local: bool = False
    allowed_hosts: frozenset = frozenset()

    @classmethod
    def public(cls):
        return cls(kind=KIND_PUBLIC)

    @classmethod
    def trusted_private(cls, *, allow_private_networks=True, allowed_hosts=(), allow_loopback=False,
                        allow_link_local=False):
        return cls(kind=KIND_TRUSTED_PRIVATE, allow_private_networks=allow_private_networks,
                   allow_hostnames=True, allow_loopback=allow_loopback, allow_link_local=allow_link_local,
                   allowed_hosts=frozenset(str(host).lower().rstrip('.') for host in allowed_hosts))

    def check_address(self, address):
        try:
            ip = ipaddress.ip_address(str(address).strip().strip('[]'))
        except ValueError:
            return Decision(False, REASON_NOT_PUBLIC, str(address))
        if ip.is_loopback:
            return (Decision(True, REASON_ALLOWED, ip.compressed) if self.allow_loopback
                    else Decision(False, REASON_LOOPBACK, ip.compressed))
        if ip.is_link_local:
            # 169.254.169.254 and friends are not a proxy destination just because a
            # collection is trusted, so this stays a second, explicit opt-in.
            return (Decision(True, REASON_PRIVATE_ALLOWED, ip.compressed) if self.allow_link_local
                    else Decision(False, REASON_NOT_PUBLIC, ip.compressed))
        if self.kind == KIND_PUBLIC and not ip.is_global:
            return Decision(False, REASON_NOT_PUBLIC, ip.compressed)
        if ip.is_private or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            if not self.allow_private_networks:
                return Decision(False, REASON_NOT_PUBLIC, ip.compressed)
            return Decision(True, REASON_PRIVATE_ALLOWED, ip.compressed)
        return Decision(True, REASON_ALLOWED, ip.compressed)

    def check_endpoint(self, endpoint):
        if endpoint.ip_version is not None:
            return self.check_address(endpoint.host)
        host = endpoint.host.lower().rstrip('.')
        if self.kind != KIND_TRUSTED_PRIVATE and not self.allow_hostnames:
            return Decision(False, REASON_HOSTNAME, host)
        if self.allowed_hosts and host not in self.allowed_hosts:
            return Decision(False, REASON_HOST_NOT_ALLOWED, host)
        return Decision(True, REASON_ALLOWED, host)

    def check_resolved(self, host, addresses):
        """Every address a hostname resolves to must pass, not just the first one.

        This is the DNS rebind guard: credentials are handed over only after all
        candidates were checked.
        """
        endpoint = self.check_endpoint(Endpoint(scheme='http', host=host, port=0))
        if not endpoint.allowed:
            return Decision(False, endpoint.reason, host)
        for address in addresses:
            verdict = self.check_address(address)
            if not verdict.allowed:
                return Decision(False, REASON_DNS_FALLBACK, verdict.address)
        return Decision(True, REASON_ALLOWED, host)


def authorize_endpoint(policy, endpoint, *, resolver=None, resolved=()):
    """Decide whether an access may be used against an endpoint.

    With a hostname and a resolver, the resolver is consulted first and every
    answer is checked; the caller passes the secret to the transport only when
    this returns an allowed decision.
    """
    if endpoint.is_hostname and resolver is not None:
        return policy.check_resolved(endpoint.host, resolver(endpoint.host))
    if endpoint.is_hostname and resolved:
        return policy.check_resolved(endpoint.host, resolved)
    return policy.check_endpoint(endpoint)


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #

_SECRET_KEYS = ('password', 'passwd', 'pwd', 'secret', 'token', 'userinfo',
                'proxy_password', 'api_key', 'authorization')


def describe(access, *, state=None):
    """The only shape an access may take in a log line, an event or an API row.

    CONTRACTS §4.4 and §5.6: `access_id` travels without the credential, and the
    reference is an opaque random handle, not an encoded secret.
    """
    return {
        'access_id': access.id,
        'endpoint_id': access.endpoint_id,
        'mode': access.mode,
        'access_revision': access.access_revision,
        'secret_ref': access.secret_ref,
        'secret_state': state,
        'rotated_at': access.rotated_at,
    }


def scrub_mapping(mapping):
    """Copy a mapping with anything that looks like a secret removed.

    Keys are matched by name, and string values additionally lose any userinfo a
    proxy URL may carry: an error message or a `proxy` field is exactly where a
    credential sneaks out under an innocent key.
    """
    if not isinstance(mapping, dict):
        return {}
    clean = {}
    for key, value in mapping.items():
        if any(marker in str(key).lower() for marker in _SECRET_KEYS):
            clean[key] = REDACTED
        elif isinstance(value, dict):
            clean[key] = scrub_mapping(value)
        elif isinstance(value, str):
            clean[key] = redact_proxy_url(value)
        else:
            clean[key] = value
    return clean


def redact_text(text, secrets):
    """Replace known secret values in a message before it is logged.

    Transport errors quote the proxy URL, and that URL carries the credential, so
    every line that can contain one goes through here.
    """
    if not isinstance(text, str):
        return text
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


def redact_proxy_url(url, secrets=()):
    """Strip userinfo from a URL, for logs and diagnostics."""
    if not isinstance(url, str) or '://' not in url:
        return url
    scheme, _, rest = url.partition('://')
    if '@' not in rest:
        return redact_text(url, secrets)
    return '%s://%s' % (scheme, rest.rsplit('@', 1)[1])


def log_fields(access, *, state=None, **extra):
    """Log-ready fields. Every value is scrubbed again on the way out."""
    fields = describe(access, state=state)
    fields.update(scrub_mapping(extra))
    return fields


def _excerpt(value, limit=64):
    """A short rendering of untrusted input for an error message."""
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= limit else text[:limit] + '...'
