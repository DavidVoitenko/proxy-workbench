"""Rotating proxy gateway: one local address that spreads client connections over measured upstreams.

Browsers, scripts and apps point at ``127.0.0.1:8899`` as an HTTP or SOCKS5
proxy.  LAN access is an explicit opt-in with its own interface choice and its
own password; the wildcard address itself is never used as a QR target.
SOCKS5 CONNECT is implemented, while UDP ASSOCIATE is intentionally rejected
because the local relay has no authenticated UDP path.

Rules this module keeps, because each of them was a defect or a requirement:

* a concurrency slot is **reserved before** the first ``await`` that can block
  and is released on success, on error and on cancellation (defect 16);
* one absolute deadline covers the whole client handshake, not just the first
  byte, and the number of accepted clients is bounded (defect 16, R11);
* an open TCP stream is never moved to another upstream: the request that was
  already written to one upstream is never written to another (F16, defect 17);
* ``connected`` is not ``working``: only bytes from the target prove an HTTP
  upstream works, and a target that refused is not the proxy's fault
  (defect 17);
* the local denylist is applied **before** any network call, and a new rule
  revokes new admissions immediately; the fate of already open streams is a
  separate, explicit ``on_deny`` policy (defect 19, R13);
* the gateway password is a separate identity from the GUI session token and
  the API token, and the default bind stays on loopback (defect 18, R12,
  CONTRACTS §5.1);
* an upstream credential is produced only by :mod:`proxy_workbench.secrets`
  and lives only for the handshake that needs it, and a rejected credential is
  **indistinguishable** from a missing one and from a dead proxy (F04, defect 1
  of the area list: the listener used to speak to a password-protected proxy
  without ever authenticating, so every such address failed forever).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import hmac
import inspect
import ipaddress
import json
import random
import re
import secrets
import socket
import ssl
import struct
import threading
import time
from collections import OrderedDict
from pathlib import Path

from . import geoip, reputation, secrets as secretstore, socks4
from .api import Exports, is_loopback, select
from .i18n import tr

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 8899
#: Every upstream transport the gateway can speak.  ``socks5h`` resolves the
#: name at the proxy, ``socks4a`` carries a hostname to a SOCKS4 proxy and
#: ``https`` wraps the connection to the proxy itself in TLS.
SUPPORTED = ('http', 'https', 'socks4', 'socks4a', 'socks5', 'socks5h')
#: Transports whose DNS mode is "the proxy resolves the name".
REMOTE_DNS = ('socks5h', 'socks4a')
STRATEGIES = ('round-robin', 'random', 'health-aware')
STICKY_MODES = ('failover', 'strict')
#: What happens to streams that are already open when a proxy is denied.
REVOKE_POLICIES = ('keep', 'close')
MAX_HEAD = 64 * 1024
SESSION = re.compile(r'[A-Za-z0-9_]{1,64}')
HOP_HEADERS = {b'proxy-authorization', b'proxy-connection', b'connection', b'keep-alive'}
#: A client request may name a listener binding with ``pool-<id>`` in its user
#: name.  Every other client option only narrows the bound scope.
POOL_OPTION = re.compile(r'[A-Za-z0-9._-]{1,64}')

# Health outcomes.  Only the ones in HEALTH_FAULT are a fault of the proxy
# itself; a target that refused must never rest a working upstream.
HEALTH_GOOD = frozenset({'response', 'tunnel_bytes'})
HEALTH_TARGET = frozenset({'upstream_unavailable', 'no_response'})
HEALTH_FAULT = frozenset({'handshake_failed', 'upstream_refused', 'closed_empty'})
HEALTH_OUTCOMES = HEALTH_GOOD | HEALTH_TARGET | HEALTH_FAULT
#: Half-life of a gateway health observation, seconds.
HEALTH_DECAY = 300.0
#: Laplace prior, so a proxy nobody tried yet is neither praised nor punished.
HEALTH_PRIOR = 1.0
CACHE_LIMIT = 256
SESSIONS_LIMIT = 10_000
MAX_CLIENTS = 512
#: The one word recorded for an upstream the gateway could not use.
#:
#: A refused connection, a handshake that answered with garbage, a ``407`` and a
#: wrong password are the same verdict on purpose: an observer of the pool must
#: not be able to learn *why* one address is unusable, because "the proxy asked
#: for a password we do not have" and "the proxy rejected the password we have"
#: are the same kind of secret - whether the gateway holds a credential for a
#: given address (see ``AccessCredentials``, and the ``407`` section of
#: docs/integration/HANDOFF/area-gateway.md for the client-visible half).
UNUSABLE = 'unusable'
#: The wildcard a LAN listener binds when no address was named.  It is never a
#: published address: :func:`display_host` replaces it with a real interface.
LAN_WILDCARD_V4 = '0.0.0.0'
LAN_WILDCARD_V6 = '::'


class UpstreamError(Exception):
    pass


def new_gateway_token():
    """A fresh password for gateway clients.

    The gateway password is a separate identity from the GUI session token and
    from the API token (CONTRACTS §5.1, defect 18).  This function never reads
    either of them, so a LAN phone password can never become a control secret.
    """
    return secrets.token_urlsafe(24)


def resolve_token(bind, token=None):
    """The gateway password for a bind: explicit, freshly generated, or none.

    On a LAN bind a password is mandatory, and when the caller did not supply
    one the gateway makes its own.  It never reads the GUI session token and
    never reads the API token: those are different identities (CONTRACTS §5.1,
    defect 18).  A loopback listener keeps the old behaviour, where a token is
    used when given and none is invented when not.
    """
    if bind.local:
        return token, ('explicit' if token else 'none')
    return (token or new_gateway_token()), ('explicit' if token else 'generated')


def local_interface_addresses():
    """Return routed IPv4 addresses without resolving this machine's hostname."""
    from .local_network import route_addresses
    return {address for address in route_addresses() if ipaddress.ip_address(address).version == 4}


def lan_interfaces():
    """Concrete addresses a phone on the same LAN could dial, best first.

    Local route metadata only: no DNS, packets, scan or third-party service.
    """
    found = []
    for candidate in sorted(local_interface_addresses()):
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if address.is_loopback or address.is_link_local or not address.is_private:
            continue
        if candidate not in found:
            found.append(candidate)
    return found


def display_host(bind_host, interface=None):
    """Return an address a phone on the same LAN can actually dial.

    Binding to ``0.0.0.0`` is useful for a desktop acting as a phone gateway,
    but the wildcard address itself is not a valid QR target.  An explicit
    ``interface`` wins, so the user can choose which adapter is published.
    """
    if interface:
        return interface
    if bind_host not in ('0.0.0.0', '::', ''):
        return bind_host
    found = lan_interfaces()
    return found[0] if found else DEFAULT_HOST


@dataclasses.dataclass(frozen=True)
class Bind:
    """Where a listener listens and what is visible about it.

    ``lan`` is the explicit opt-in.  Without it only loopback is allowed, so
    the local default stays local and a phone needs a deliberate decision.

    ``host`` is what the caller *asked for*; :attr:`listen_host` is the address
    the socket really binds.  They differ in exactly one case, and the case is
    the whole point of the opt-in: LAN with a loopback address means "open me
    to the network", so the listener binds the wildcard and publishes a concrete
    interface (:attr:`published_host`).  A caller that asked for a wildcard
    without ``lan`` is refused in :meth:`__post_init__` instead.
    """

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    lan: bool = False
    interface: str | None = None

    def __post_init__(self):
        if not 0 <= int(self.port) <= 65535:
            raise ValueError('port')
        if not is_loopback(self.host) and not self.lan:
            raise ValueError(tr(
                f'Адрес {self.host} доступен из сети: LAN включается явно (bind.lan=True) и с выбором интерфейса.',
                f'the address {self.host} is reachable from the network: enable LAN explicitly '
                f'(bind.lan=True) and choose an interface.'))
        if self.interface and is_loopback(self.interface):
            raise ValueError(tr('Интерфейс LAN не может быть loopback.',
                                'the LAN interface cannot be a loopback address.'))

    @property
    def listen_host(self):
        """The address ``asyncio.start_server`` is given.

        With LAN on and no address of its own, the listener takes the wildcard
        for the family the requested loopback address belongs to, so
        ``--lan`` is a real opt-in rather than a flag that changes nothing.
        With an interface chosen, only that address is bound.
        """
        if not self.lan:
            return self.host
        if self.interface:
            return self.interface
        if is_loopback(self.host):
            return LAN_WILDCARD_V6 if ':' in self.host else LAN_WILDCARD_V4
        return self.host

    @property
    def local(self):
        """Whether the listener is reachable only from this computer."""
        return is_loopback(self.listen_host)

    @property
    def published_host(self):
        """The address to hand to a client; loopback while LAN is off.

        Derived from :attr:`listen_host`, not from ``host``: a LAN opt-in that
        kept the default loopback address binds the wildcard, and publishing
        ``127.0.0.1`` for it would hand a phone an address it cannot reach.
        """
        return display_host(self.listen_host, self.interface) if self.lan else self.host

    def as_dict(self):
        return dict(host=self.host, port=int(self.port), lan=self.lan, interface=self.interface,
                    listen_host=self.listen_host, local=self.local,
                    published_host=self.published_host if self.lan else self.host)


SCOPING_KEYS = ('protocol', 'countries', 'anonymity', 'max_latency')


@dataclasses.dataclass(frozen=True)
class Binding:
    """Which pool, generation, profile and policy one listener or client serves.

    A binding pins the generation and profile so publishing another run does
    not silently move an already connected client (defect 8).  ``policy`` holds
    per-binding knobs: ``max_per_proxy``, ``sticky``, ``strategy`` and the
    scoping filters ``protocol``/``countries``/``anonymity``/``max_latency``,
    which narrow the rows a client may ever be offered.
    """

    pool_id: str = 'default'
    generation: str | None = None
    profile_id: str | None = None
    profile_revision: int | None = None
    policy: dict = dataclasses.field(default_factory=dict)

    def as_dict(self):
        data = dataclasses.asdict(self)
        data['policy'] = {key: (list(value) if isinstance(value, tuple) else value)
                          for key, value in self.policy.items()}
        return data

    def option(self, name, fallback=None):
        value = self.policy.get(name, fallback)
        return fallback if value is None else value

    def scope(self):
        """The filters this binding applies, whatever a client asks for."""
        scope = {}
        for key in SCOPING_KEYS:
            value = self.policy.get(key)
            if value is None:
                continue
            scope[key] = tuple(value) if key == 'countries' and not isinstance(value, str) else value
        return scope


class AccessCredentials:
    """A row's access identity, resolved into the bytes one transport needs.

    This is the F04 path the listener was missing: an endpoint that was
    imported with its own username/password is dialled with that credential, so
    ``import -> worker check -> scoped gateway`` is a real chain instead of a
    promise.  Three rules keep it honest:

    * the value is built by :func:`proxy_workbench.secrets.transport_credentials`
      and by nothing else - the gateway has no second place that formats a
      password, so no URL, log line, snapshot or exception can carry one;
    * it is resolved for the exact ``(access_id, access_revision)`` the row
      names, so a rotated password can never inherit the old one's evidence and
      a superseded revision is refused rather than sent;
    * an endpoint with *several* usable access identities is ambiguous, and an
      ambiguous row gets no credential at all.  Two credentials of one address
      are two identities (CONTRACTS §1.2(1)); merging them would be worse than
      not authenticating.

    Nothing here raises into the relay path.  A locked vault, a missing secret,
    a wrong scheme or an ambiguous row all end as "no credential", and the
    upstream then simply fails like any other unusable address - see
    ``UNUSABLE`` for why that indistinguishability is deliberate.
    """

    def __init__(self, store, *, lock=None):
        #: ``secrets.AccessStore``; its connection must allow the calling
        #: thread, because the listener resolves off the event loop.
        self.store = store
        self.lock = lock or threading.RLock()
        #: Local diagnostics only.  Deliberately absent from ``snapshot()``,
        #: which the listener serves to anybody who may ask it.
        self.problems = 0
        self.resolved = 0

    def _candidate(self, row):
        """The access this row was measured with, or a refusal.

        A row that names its access is taken at its word.  A row that only
        names an endpoint - which is what ``api.public_row`` produces today -
        is authenticated only when that endpoint has exactly one usable access;
        several means ambiguous, because two credentials of one address are two
        identities and merging them is worse than not authenticating.
        """
        access_id = row.get('access_id')
        if access_id:
            return self.store.require(access_id)
        endpoint_id = row.get('endpoint_id')
        if not endpoint_id:
            # No reference at all: this row was never measured with a
            # credential, which is the ordinary open-proxy case and not a fault.
            return None
        candidates = [access for access in self.store.list_for_endpoint(endpoint_id)
                      if self.store.usable(access)]
        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            raise secretstore.SecretConflictError(
                'endpoint %s has more than one usable access' % endpoint_id,
                action='let the row name the access it was measured with')
        raise secretstore.SecretNotProvided(
            'endpoint %s has no usable access' % endpoint_id,
            action='re-enter the credential for this access')

    def __call__(self, row, scheme):
        with self.lock:
            try:
                access = self._candidate(row)
                if access is None:
                    return None
                resolved = self.store.resolve(access.id, access_revision=row.get('access_revision'))
                try:
                    credentials = secretstore.transport_credentials(access, resolved, scheme=scheme)
                finally:
                    resolved.scrub()
                if not credentials.authenticated:
                    return None
                self.resolved += 1
                return credentials
            except secretstore.SecretError:
                self.problems += 1
                return None

    def as_json(self):
        """A describable form for the local operator; never a credential."""
        return dict(resolved=self.resolved, unavailable=self.problems)


class Lease:
    """A held concurrency slot for one proxy.

    Released exactly once, whether the connection succeeded, failed or was
    cancelled (defect 16).  ``with lease:`` is the safe form.  ``credentials``
    rides along only until the request that needs it has been written.
    """

    __slots__ = ('pool', 'proxy', 'taken_at', 'credentials', '_released')

    def __init__(self, pool, proxy, now=None):
        self.pool = pool
        self.proxy = proxy
        self.taken_at = time.monotonic() if now is None else now
        self.credentials = None
        self._released = False

    @property
    def released(self):
        return self._released

    def release(self):
        self.credentials = None
        if self._released:
            return
        self._released = True
        self.pool.release(self.proxy)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


class ReplayGuard:
    """One client request is written to at most one upstream.

    A gateway may try several upstreams while nothing of the request has been
    sent.  The moment the first byte of the request goes out, the request
    belongs to that upstream: a non-idempotent request is never repeated on
    another one (defect 17).  The guard makes that structural - there is no
    code path that can write the same request twice.
    """

    __slots__ = ('sent_to', 'bytes_written')

    def __init__(self):
        self.sent_to = None
        self.bytes_written = 0

    def write_once(self, lease, writer, payload):
        if self.sent_to is not None:
            raise UpstreamError('REPLAY_REFUSED')
        self.sent_to = lease.proxy
        self.bytes_written = len(payload)
        writer.write(payload)


class Pool:
    """Working proxies from one export with rotation, sessions, reservations and health.

    The pool is the gateway's only view of an upstream.  It decides *which*
    proxy, never whether a request succeeded: the gateway reports outcomes and
    the pool turns them into rotation, cool-down and health-aware order.
    """

    def __init__(self, data, filters=None, strategy='round-robin', max_failures=2, cooldown=300,
                 max_per_proxy=0, session_ttl=600, *, sticky='failover', denylist=None,
                 denylist_path=None, denylist_normalizer=None, on_deny='keep',
                 cache_limit=CACHE_LIMIT, bindings=None, default_binding=None,
                 healthy_latency_ms=0):
        if strategy not in STRATEGIES:
            raise ValueError('strategy')
        if sticky not in STICKY_MODES:
            raise ValueError('sticky')
        if on_deny not in REVOKE_POLICIES:
            raise ValueError('on_deny')
        if int(cache_limit) < 1:
            raise ValueError('cache_limit')
        self.data = Path(data)
        self.exports = Exports(self.data / 'exports')
        self.filters = {'protocol': 'all', 'countries': (), 'anonymity': 'any', 'max_latency': 0, **(filters or {})}
        self.strategy = strategy
        self.max_failures = max_failures
        self.cooldown = cooldown
        self.max_per_proxy = max_per_proxy
        self.session_ttl = session_ttl
        self.sticky = sticky
        self.on_deny = on_deny
        self.cache_limit = int(cache_limit)
        self.healthy_latency_ms = healthy_latency_ms
        self.denylist_path = Path(denylist_path) if denylist_path else self.data / 'denylist.txt'
        self.denylist_normalizer = denylist_normalizer
        self.denylist = denylist if denylist is not None else reputation.Denylist.empty()
        self.bindings = dict(bindings or {})
        self.default_binding = default_binding or Binding()
        self.lock = threading.RLock()
        self.failures = {}
        self.resting = {}
        self.active = {}
        self.usage = {}
        self.health = {}
        #: The last fault note per proxy; local diagnostics, never published.
        self.reasons = {}
        self.sessions = {}
        self.position = 0
        self.key = None
        self.revision = None
        self.generation = None
        self.status = {}
        #: What the export yielded after transport and filter selection.
        self.source_rows = []
        #: ``source_rows`` minus the denylist: what a client may be offered.
        self.rows = []
        #: ``proxy`` -> row, for everything in :attr:`rows`.
        self.index = {}
        #: (export key, export revision, denylist digest) the rows were built from.
        self.rows_stamp = None
        self.cache = OrderedDict()
        self.denied = set()
        self.revoked = 0
        self.stats = dict(connections=0, failed=0, retries=0, rejected=0, upstream_5xx=0,
                          closed_idle=0, closed_deadline=0, capped=0, denied=0, replay_refused=0)

    # --- export and denylist -------------------------------------------------

    def load_denylist(self):
        """Re-read data/denylist.txt.  Called off the event loop (see ``Gateway``)."""
        path = self.denylist_path
        try:
            stat = path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp = None
        if stamp == getattr(self, '_denylist_stamp', object()):
            return self.denylist
        try:
            denylist = reputation.Denylist.from_file(path, normalizer=self.denylist_normalizer)
        except OSError:
            denylist = reputation.Denylist.empty()
        self._denylist_stamp = stamp
        return self.set_denylist(denylist)

    def set_denylist(self, denylist):
        """Install a denylist and revoke admissions for everything it now names.

        Revoking an admission is immediate and complete: the proxy leaves the
        servable row list and the filter cache, so no later request can pick it,
        and it is never dialled.  Removing a rule puts the proxy back, because
        the servable list is always *derived* from the export instead of being
        edited in place - a one-way ratchet that only a new publication could
        undo would silently shrink the pool for good (defect 19).
        Streams that are already open are a separate decision - see
        ``on_deny`` and :meth:`revoke_streams`.
        """
        denylist = denylist if denylist is not None else reputation.Denylist.empty()
        with self.lock:
            self.denylist = denylist
            self._apply_denylist()
        return self.denylist

    def _apply_denylist(self):
        """Recompute the servable rows from the export and the current denylist.

        Caller holds the lock.  The deny decision is taken from the denylist
        alone, so the same call both revokes a new rule and restores a removed
        one, and the ``denied`` set always means "named right now" rather than
        "named at some point".  The stamp makes the common case - a refresh
        tick where neither the export nor the rules moved - a single comparison
        instead of a rebuild.
        """
        stamp = (self.exports.key, self.exports.revision, self.denylist.digest)
        if stamp == self.rows_stamp:
            return self.denied
        denied = {row['proxy'] for row in self.source_rows
                  if self.denylist.match(row['proxy']) is not None}
        added = denied - self.denied
        if added:
            self.revoked += len(added)
            self.stats['denied'] += len(added)
        self.rows_stamp = stamp
        self.denied = denied
        self.rows = [row for row in self.source_rows if row['proxy'] not in denied]
        # Selection hands out addresses; a credential needs the row.  The index
        # is rebuilt with the rows, so it can never name a proxy the listener
        # would not serve, and looking one up stays O(1) on every connect.
        self.index = {row['proxy']: row for row in self.rows}
        self.cache.clear()
        # Session bindings are left in place on purpose.  A denied address is
        # no longer available, so a failover session re-binds to a working one
        # and a strict session is refused - both of which the sticky mode
        # decides, not the denylist.
        return denied

    def denied_proxies(self):
        with self.lock:
            return sorted(self.denied)

    def allowed(self, proxy):
        """False when the local denylist names this proxy, before any network call."""
        return self.denylist.match(proxy) is None and proxy not in self.denied

    def revoke_streams(self, force=None):
        """The proxies whose open streams the caller should close, or an empty list.

        ``keep`` is the default and it is a real decision, not an omission: an
        open stream already has bytes on the wire and cutting it is the user's
        call, not a side effect of adding a rule.  ``close`` is the explicit
        request to drop them.  ``force`` overrides the configured policy for a
        single call, which is what :meth:`Gateway.set_denylist` needs.
        """
        policy = self.on_deny if force is None else ('close' if force else 'keep')
        with self.lock:
            return sorted(self.denied) if policy == 'close' else []

    def refresh(self):
        """Re-read the export and the denylist.  Performs file I/O: not for the event loop."""
        self.load_denylist()
        rows, status = self.exports.load()
        with self.lock:
            self.status = status
            if self.exports.key != self.key or self.exports.revision != self.revision:
                self.key = self.exports.key
                self.revision = self.exports.revision
                self.generation = (status or {}).get('generation')
                # The URL scheme is the transport, so https://, socks4a and
                # socks5h are first-class rows now, not filtered away.
                self.source_rows = [row for row in select(rows, self.filters)
                                    if str(row['proxy']).partition('://')[0] in SUPPORTED]
            # Always re-derived, so a rule that was removed lets its proxy back.
            self._apply_denylist()
            # What this listener would serve right now: the denylist and the
            # binding of the default pool.  A named binding narrows from here.
            return self.matching(binding=self.default_binding)

    async def arefresh(self):
        """Refresh off the event loop, so the listener never blocks on a file read."""
        return await asyncio.to_thread(self.refresh)

    # --- selection -----------------------------------------------------------

    def binding_for(self, name=None):
        """The binding a listener or a client serves.  ``default`` is always addressable."""
        if name is None or name == 'default':
            return self.default_binding
        binding = self.bindings.get(name)
        if binding is None:
            raise KeyError(name)
        return binding

    def _bound_rows(self, binding):
        rows = self.rows
        if binding is None:
            return rows
        if binding.generation and binding.generation != self.generation:
            return []
        if binding.profile_id:
            # A pinned profile is served only from an export that declares it;
            # an export without a profile cannot prove the scope, so it fails closed.
            if (self.status or {}).get('profile') != binding.profile_id:
                return []
        if binding.profile_revision:
            if (self.status or {}).get('profile_revision') not in (None, binding.profile_revision):
                return []
        scope = binding.scope()
        if not scope:
            return rows
        return select(rows, {'protocol': 'all', 'countries': (), 'anonymity': 'any',
                             'max_latency': 0, **scope})

    def matching(self, request=None, binding=None):
        """Proxies allowed by the binding, the gateway filters and the denylist.

        Pure in-memory: the export is read by :meth:`refresh` (or
        :meth:`arefresh` off the event loop), never here.
        """
        with self.lock:
            source = self._bound_rows(binding)
            if not request:
                return [row['proxy'] for row in source]
            key = (binding.pool_id if binding else '', *sorted(request.items()))
            cached = self.cache.get(key)
            if cached is not None:
                self.cache.move_to_end(key)
                return cached
            query = {'protocol': 'all', 'countries': (), 'anonymity': 'any', 'max_latency': 0, **request}
            value = [row['proxy'] for row in select(source, query)]
            self.cache[key] = value
            while len(self.cache) > self.cache_limit:
                self.cache.popitem(last=False)
            return value

    def available(self, request=None, now=None, binding=None):
        now = time.monotonic() if now is None else now
        limit = self._limit(binding)
        with self.lock:
            return [proxy for proxy in self.matching(request, binding)
                    if self.resting.get(proxy, 0) <= now and self.allowed(proxy)
                    and (not limit or self.active.get(proxy, 0) < limit)]

    def row_for(self, proxy):
        """The published row behind one chosen proxy, or None.

        Selection deals in addresses, but a credential belongs to the *access
        identity* a row was measured with, so the relay looks the row up here
        (F04).  Pure in-memory and O(1); a row that is not servable is not
        served, so a denied proxy resolves to nothing.
        """
        with self.lock:
            return self.index.get(proxy)

    def _limit(self, binding):
        if binding is not None and binding.policy.get('max_per_proxy'):
            return int(binding.policy['max_per_proxy'])
        return self.max_per_proxy

    def _strategy(self, binding):
        if binding is not None and binding.policy.get('strategy') in STRATEGIES:
            return binding.policy['strategy']
        return self.strategy

    def _sticky(self, binding, sticky):
        if sticky:
            return sticky
        if binding is not None and binding.policy.get('sticky') in STICKY_MODES:
            return binding.policy['sticky']
        return self.sticky

    def health_score(self, proxy, now=None):
        """Decayed success ratio with a Laplace prior, in ``(0, 1)``.

        A proxy nobody tried yet scores ``0.5`` - neither praised for a
        measurement the gateway never made nor punished for one.  Observations
        lose half their weight every ``HEALTH_DECAY`` seconds, so an address
        that stopped working falls out of the ranking on its own.  Only genuine
        upstream faults count against a proxy; a target that refused does not.
        """
        now = time.monotonic() if now is None else now
        record = self.health.get(proxy)
        if not record:
            return 0.5
        weight = 0.5 ** (max(0.0, now - record['at']) / HEALTH_DECAY)
        good = record['ok'] * weight + HEALTH_PRIOR
        bad = record['failed'] * weight + HEALTH_PRIOR
        score = good / (good + bad)
        if self.healthy_latency_ms and record.get('connect_ms'):
            penalty = min(0.25, record['connect_ms'] / (4 * self.healthy_latency_ms))
            score *= 1 - penalty
        return score

    def _choose(self, candidates, strategy, now):
        if strategy == 'random':
            return random.choice(candidates)
        self.position = (self.position + 1) % len(candidates)
        if strategy == 'health-aware':
            # Best observed health wins.  Equal scores - which is every proxy the
            # gateway has not measured yet, and every proxy whose target happened
            # to be down - fall back to the round-robin cursor, so one address
            # cannot starve the rest before any evidence exists.
            start = self.position
            return min(candidates, key=lambda proxy: (-round(self.health_score(proxy, now), 6),
                                                      (candidates.index(proxy) - start) % len(candidates)))
        return candidates[self.position]

    def _session(self, session, now):
        proxy, expires = self.sessions.get(session, (None, 0))
        return proxy if expires > now else None

    def _prune_sessions(self, now):
        if len(self.sessions) <= SESSIONS_LIMIT:
            return
        # Expired bindings go first; if that is not enough the ones that expire
        # soonest go next, so the table is bounded by a number, not by uptime.
        self.sessions = {key: value for key, value in self.sessions.items() if value[1] > now}
        if len(self.sessions) > SESSIONS_LIMIT:
            keep = sorted(self.sessions.items(), key=lambda item: -item[1][1])[:SESSIONS_LIMIT]
            self.sessions = dict(keep)

    def pick(self, exclude=(), request=None, session=None, sticky=None, binding=None):
        """Choose a proxy without taking a slot.  Prefer :meth:`reserve` in a gateway."""
        return self._pick(exclude, request, session, sticky, binding, reserve=False)

    def reserve(self, request=None, exclude=(), session=None, sticky=None, binding=None):
        """Pick a proxy and take its concurrency slot in one indivisible step.

        Doing both together is what keeps parallel connects under
        ``max_per_proxy``: there is no await between the check and the
        reservation, so two clients can never observe the same free slot
        (defect 16).  The caller owns the returned :class:`Lease`.
        """
        return self._pick(exclude, request, session, sticky, binding, reserve=True)

    def _pick(self, exclude, request, session, sticky, binding, reserve):
        now = time.monotonic()
        sticky = self._sticky(binding, sticky)
        with self.lock:
            self._prune_sessions(now)
            candidates = [proxy for proxy in self.available(request, now, binding) if proxy not in exclude]
            if session:
                pinned = self._session(session, now)
                if pinned and pinned in candidates:
                    if reserve:
                        self._take(pinned)
                        self.sessions[session] = (pinned, now + self.session_ttl)
                        return Lease(self, pinned)
                    return pinned
                if pinned and sticky == 'strict':
                    # A strict session never silently changes its address: no
                    # free slot for the proxy it is bound to means a refusal.
                    return None
            if not candidates:
                return None
            choice = self._choose(candidates, self._strategy(binding), now)
            if session:
                self.sessions[session] = (choice, now + self.session_ttl)
            if not reserve:
                return choice
            self._take(choice)
            return Lease(self, choice)

    def _take(self, proxy):
        self.active[proxy] = self.active.get(proxy, 0) + 1

    def acquire(self, proxy):
        """Kept for callers that reserve by hand; :meth:`reserve` is safer."""
        with self.lock:
            self._take(proxy)

    def release(self, proxy):
        with self.lock:
            if self.active.get(proxy, 0) > 1:
                self.active[proxy] -= 1
            else:
                self.active.pop(proxy, None)

    def active_for(self, proxy):
        return self.active.get(proxy, 0)

    # --- health --------------------------------------------------------------

    def connected(self, proxy, connect_ms=0.0):
        """The upstream accepted a connection and finished its handshake.

        This is deliberately *not* a success and it does not clear a failure
        streak: nothing of the target has been proved yet (defect 17).  Only an
        outcome that carried traffic does that.
        """
        with self.lock:
            record = self.health.setdefault(proxy, {'ok': 0.0, 'failed': 0.0, 'target': 0, 'at': 0.0, 'connect_ms': 0.0})
            record['connect_ms'] = float(connect_ms)
            usage = self.usage.setdefault(proxy, {'ok': 0, 'failed': 0, 'target_failed': 0})
            usage.setdefault('target_failed', 0)

    def outcome(self, proxy, kind, detail=None):
        """Record what the exchange with one upstream actually proved."""
        if kind not in HEALTH_OUTCOMES:
            raise ValueError(kind)
        with self.lock:
            usage = self.usage.setdefault(proxy, {'ok': 0, 'failed': 0, 'target_failed': 0})
            record = self.health.setdefault(proxy, {'ok': 0.0, 'failed': 0.0, 'target': 0, 'at': 0.0, 'connect_ms': 0.0})
            if kind in HEALTH_GOOD:
                usage['ok'] += 1
                record['ok'] += 1
                record['at'] = time.monotonic()
                self.failures.pop(proxy, None)
            elif kind in HEALTH_TARGET:
                # The upstream answered: the target refused.  Not the proxy's fault.
                usage['target_failed'] += 1
                record['target'] += 1
            else:
                usage['failed'] += 1
                record['failed'] += 1
                record['at'] = time.monotonic()
                self.failures[proxy] = self.failures.get(proxy, 0) + 1
                if self.failures[proxy] >= self.max_failures:
                    self.resting[proxy] = time.monotonic() + self.cooldown
                    self.failures.pop(proxy)
            if detail:
                # Kept beside the counters, deliberately *not* in the usage
                # record that ``report()`` and the status page publish: a
                # fault recorded while dialling and a fault recorded after the
                # tunnel was up would otherwise be told apart by the mere
                # presence of a word, which is the same leak as a different
                # counter (see ``UNUSABLE``).
                self.reasons[proxy] = detail

    def reason(self, proxy):
        """The local note on the last fault of one proxy, or None.

        For an operator reading a log.  Never published: ``report()`` and
        ``snapshot()`` stay identical for every unusable upstream.
        """
        with self.lock:
            return self.reasons.get(proxy)

    def ok(self, proxy):
        """A target answered: the only evidence that counts as working."""
        self.outcome(proxy, 'response')

    def failed(self, proxy):
        """A fault of the upstream itself."""
        self.outcome(proxy, 'handshake_failed')

    def report(self, proxy):
        with self.lock:
            return dict(self.usage.get(proxy, {'ok': 0, 'failed': 0, 'target_failed': 0}))

    # --- visibility ----------------------------------------------------------

    def snapshot(self, top=20):
        """Counters only: no file access, so it is safe from any thread.

        The event loop must use :meth:`asnapshot` when it wants a fresh export;
        a blocking read on the loop is what made the old status page stall
        every client (F16, "safe snapshot from the event loop").
        """
        now = time.monotonic()
        with self.lock:
            busiest = sorted(self.usage.items(), key=lambda item: (-item[1]['ok'], item[1]['failed']))[:top]
            return dict(self.stats,
                        proxies=len(self.rows), available=len(self.available(now=now)),
                        resting=sum(until > now for until in self.resting.values()),
                        sessions=sum(expires > now for _, expires in self.sessions.values()),
                        active=sum(self.active.values()),
                        denied=len(self.denied), revoked=self.revoked,
                        generation=self.generation,
                        binding=self.default_binding.as_dict(),
                        strategy=self.strategy, sticky=self.sticky, on_deny=self.on_deny,
                        denylist_rules=self.denylist.rules, denylist_digest=self.denylist.digest,
                        top=[dict(proxy=proxy, active=self.active.get(proxy, 0),
                                  health=round(self.health_score(proxy, now), 3), **counts)
                             for proxy, counts in busiest])

    async def asnapshot(self, top=20):
        await self.arefresh()
        return self.snapshot(top)

    def state(self):
        """Everything a GUI or an API needs to show the binding and the policy."""
        with self.lock:
            return dict(self.snapshot(), filters=dict(self.filters),
                        bindings={name: value.as_dict() for name, value in self.bindings.items()},
                        profile=self.status.get('profile') if self.status else None,
                        export_state=(self.status or {}).get('state'),
                        export_valid_until=(self.status or {}).get('valid_until'))


def client_options(username):
    """Per-client choices carried in the proxy user name, e.g. ``country-de_nl-protocol-socks5-session-a1``."""
    parts = [part for part in (username or '').split('-') if part]
    request, session, pool = {}, None, None
    for key, value in zip(parts[::2], parts[1::2]):
        key = key.lower()
        if key == 'country':
            request['countries'] = geoip.parse_countries(value.replace('_', ','))
        elif key == 'protocol':
            if value.lower() not in SUPPORTED:
                raise ValueError('protocol')
            request['protocol'] = value.lower()
        elif key == 'latency':
            request['max_latency'] = float(int(value))
        elif key == 'anonymity':
            if value.lower() not in ('anonymous', 'elite'):
                raise ValueError('anonymity')
            request['anonymity'] = value.lower()
        elif key == 'session':
            if not SESSION.fullmatch(value):
                raise ValueError('session')
            session = value
        elif key == 'pool':
            if not POOL_OPTION.fullmatch(value):
                raise ValueError('pool')
            pool = value
    return request, session, pool


async def resolve(host, port, family=socket.AF_UNSPEC):
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        pass
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, family=family, type=socket.SOCK_STREAM)
    if not infos:
        raise UpstreamError('DNS')
    return infos[0][4][0]


async def read_exactly(reader, size):
    try:
        return await reader.readexactly(size)
    except asyncio.IncompleteReadError:
        raise UpstreamError('CLOSED') from None


async def read_head(reader):
    try:
        return await reader.readuntil(b'\r\n\r\n')
    except asyncio.LimitOverrunError:
        raise UpstreamError('HEAD_TOO_LARGE') from None
    except asyncio.IncompleteReadError:
        raise UpstreamError('CLOSED') from None


def http_status(head):
    """Status code of an HTTP response head, or None when this is not HTTP."""
    if not head:
        return None
    line = head.split(b'\r\n', 1)[0].split()
    if len(line) < 2 or not line[0].upper().startswith(b'HTTP/'):
        return None
    try:
        return int(line[1])
    except ValueError:
        return None


def authorization_header(credentials):
    """The ``Proxy-Authorization`` line for a resolved access, or ``''``.

    An open proxy is the common case and it gets no header at all, which
    :func:`proxy_workbench.secrets.proxy_authorization` expresses by returning
    an empty string.
    """
    value = getattr(credentials, 'proxy_authorization', '') or ''
    return 'Proxy-Authorization: %s' % value if value else ''


async def open_tunnel(proxy, host, port, forward=False, ssl_context=None, credentials=None):
    """A stream to host:port through `proxy`, after the proxy's own handshake.

    With ``forward`` an HTTP proxy gets the plain request itself instead of a
    CONNECT tunnel, since many HTTP proxies allow CONNECT only to port 443.
    An ``https://`` upstream is reached over TLS with the proxy host name as
    the server name, so certificate verification stays on.

    ``credentials`` is a :class:`proxy_workbench.secrets.TransportCredentials`,
    built by :class:`AccessCredentials` from the row this proxy was picked
    from.  It is used for the proxy's own handshake only - never for the
    target - and the caller drops it as soon as those bytes are on the wire.
    Every refusal here looks the same to the pool: a proxy that wants a
    credential we do not have, a proxy that rejects the one we sent and a proxy
    that is not an HTTP proxy at all are one verdict, ``UNUSABLE``.
    """
    scheme, _, address = proxy.partition('://')
    if scheme not in SUPPORTED:
        # Refused before any socket is opened, so an address the gateway cannot
        # speak to is never dialled.
        raise UpstreamError('UNSUPPORTED')
    proxy_host, _, proxy_port = address.rpartition(':')
    proxy_host = proxy_host.strip('[]')
    if scheme == 'https':
        context = ssl_context or ssl.create_default_context()
        reader, writer = await asyncio.open_connection(proxy_host, int(proxy_port), limit=MAX_HEAD,
                                                       ssl=context, server_hostname=proxy_host)
    else:
        reader, writer = await asyncio.open_connection(proxy_host, int(proxy_port), limit=MAX_HEAD)
    try:
        if scheme in ('http', 'https') and forward:
            pass
        elif scheme in ('http', 'https'):
            target = f'[{host}]:{port}' if ':' in host else f'{host}:{port}'
            # RFC 7231: the credential belongs to the proxy, so it rides in the
            # CONNECT the gateway makes on the client's behalf.
            lines = [f'CONNECT {target} HTTP/1.1', f'Host: {target}']
            if (header := authorization_header(credentials)):
                lines.append(header)
            writer.write(('\r\n'.join(lines) + '\r\n\r\n').encode())
            await writer.drain()
            status = http_status(await read_head(reader))
            if status != 200:
                raise UpstreamError('CONNECT_REFUSED' if status is not None else 'CONNECT_REFUSED')
        elif scheme in ('socks4', 'socks4a'):
            # SOCKS4 has no credential sub-negotiation, and F04 refuses to
            # invent one: a row whose access is not a SOCKS4 identity simply
            # gets no credential here.
            if scheme == 'socks4a':
                # SOCKS4a: DSTIP 0.0.0.x marks a hostname AFTER the
                # NUL-terminated USERID (empty for an unauthenticated row).
                name = host.encode('idna')
                if len(name) > 255:
                    raise UpstreamError('SOCKS4A_NAME_TOO_LONG')
                if not name or b'\x00' in name:
                    raise UpstreamError('SOCKS4A_BAD_NAME')
                writer.write(socks4.connect_request(b'\x00\x00\x00\x01', port) + name + b'\x00')
            else:
                ip = await resolve(host, port, socket.AF_INET)
                writer.write(socks4.connect_request(ipaddress.IPv4Address(ip).packed, port))
            await writer.drain()
            try:
                socks4.check_reply(await read_exactly(reader, socks4.REPLY_SIZE))
            except socks4.Socks4Error as exc:
                raise UpstreamError(str(exc)) from None
        elif scheme in ('socks5', 'socks5h'):
            # The offer is derived from the access, never widened: an access
            # with a credential offers only 0x02, one without only 0x00.
            greeting = getattr(credentials, 'socks5_greeting', b'') or b'\x05\x01\x00'
            writer.write(greeting)
            await writer.drain()
            answer = await read_exactly(reader, 2)
            if answer[0] != 5 or answer[1] not in (0, 2) or answer[1] not in greeting[2:]:
                raise UpstreamError('SOCKS5_AUTH')
            if answer[1] == 2:
                # RFC 1929.  A rejection ends in the same error as "no
                # acceptable method", so the pool cannot tell them apart.
                auth = getattr(credentials, 'socks5_auth', b'')
                if not auth:
                    raise UpstreamError('SOCKS5_AUTH')
                writer.write(auth)
                await writer.drain()
                if await read_exactly(reader, 2) != b'\x01\x00':
                    raise UpstreamError('SOCKS5_AUTH')
            if scheme == 'socks5':
                # socks5 resolves locally, socks5h lets the proxy do it.
                host = await resolve(host, port)
            try:
                ip = ipaddress.ip_address(host)
                target = (b'\x01' if ip.version == 4 else b'\x04') + ip.packed
            except ValueError:
                name = host.encode('idna')
                target = b'\x03' + bytes([len(name)]) + name
            writer.write(b'\x05\x01\x00' + target + struct.pack('>H', port))
            await writer.drain()
            reply = await read_exactly(reader, 4)
            if reply[0] != 5 or reply[2] != 0 or reply[3] not in (1, 3, 4):
                raise UpstreamError('SOCKS5_BAD_REPLY')
            if reply[1] != 0:
                raise UpstreamError(f'SOCKS5_REJECTED_{reply[1]}')
            skip = {1: 4, 4: 16}.get(reply[3])
            if skip is None:
                skip = (await read_exactly(reader, 1))[0]
                if not skip:
                    raise UpstreamError('SOCKS5_BAD_REPLY')
            await read_exactly(reader, skip + 2)
        else:
            raise UpstreamError('UNSUPPORTED')
    except BaseException:
        writer.close()
        raise
    return reader, writer


async def response_head(reader, timeout):
    """Read one HTTP response head and classify the answer.

    Returns ``(status, head, reason)``.  A truncated answer is returned as-is
    instead of being swallowed, so the client still sees whatever the upstream
    managed to send.  ``status`` is None when the answer is not HTTP at all,
    and ``reason`` separates "the upstream answered" from "it hung" and from
    "it closed the connection" - three different verdicts for the same silence.
    """
    data = b''
    try:
        data = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), timeout)
    except asyncio.TimeoutError:
        return None, b'', 'timeout'
    except asyncio.LimitOverrunError:
        return None, b'', 'oversized'
    except asyncio.IncompleteReadError as exc:
        data = exc.partial
    except (OSError, UpstreamError):
        return None, b'', 'closed'
    if not data:
        return None, b'', 'closed'
    return http_status(data), data, 'head'


async def pipe(reader, writer, idle, on_bytes=None, state=None):
    """Copy one direction until EOF, idle timeout or cancel.

    ``on_bytes`` is called with the running byte count after every chunk, so the
    relay can tell "the tunnel carried traffic" from "the client opened a socket
    and left" - the gateway only has evidence for the first.
    """
    total = 0
    try:
        while True:
            data = await asyncio.wait_for(reader.read(65536), idle)
            if not data:
                if state is not None:
                    state['ended'] = True
                break
            total += len(data)
            if on_bytes is not None:
                on_bytes(total)
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (OSError, asyncio.TimeoutError):
        if state is not None:
            state['ended'] = True
        writer.close()
    return total


def split_target(value, default_port):
    host, sep, port = value.rpartition(':')
    if not sep or not port.isdigit() or ']' in port:
        host, port = value, default_port
    host = host.strip('[]')
    if not host or not 0 < int(port) < 65536:
        raise ValueError('bad target')
    return host, int(port)


def credential_state(source):
    """Describe the credential source of a listener without describing a secret.

    Three shapes, because the caller may supply nothing, an
    :class:`AccessCredentials` or a source of its own: what is published is
    whether upstream authentication is possible at all and how many stored
    credentials turned out to be unusable.
    """
    if source is None:
        return dict(mode='none', resolved=0, unavailable=0)
    described = getattr(source, 'as_json', None)
    if callable(described):
        return dict(mode='access-store', **described())
    return dict(mode='external', resolved=0, unavailable=0)


class Gateway:
    def __init__(self, pool, token=None, attempts=3, connect_timeout=8, idle_timeout=300,
                 allow_local_without_auth=False, *, handshake_timeout=30, response_timeout=15,
                 max_clients=MAX_CLIENTS, max_session=0, bind=None, token_origin=None,
                 drain_timeout=2.0, ssl_context=None, refresh_interval=2.0,
                 credentials=None, binding=None):
        self.pool = pool
        self.token = token
        self.token_origin = token_origin or ('explicit' if token else 'none')
        self.attempts = max(1, int(attempts))
        self.connect_timeout = connect_timeout
        self.idle_timeout = idle_timeout
        self.allow_local_without_auth = allow_local_without_auth
        self.handshake_timeout = handshake_timeout
        self.response_timeout = response_timeout
        self.max_clients = max(1, int(max_clients))
        #: Seconds, as a float: 0 means "as long as the client is idle-free", a
        #: positive value caps the life of one relayed connection and is visible
        #: in the snapshot.  Rounded to a whole number it would silently turn
        #: every sub-second cap into "no cap at all", so the fraction is kept.
        self.max_session = max(0.0, float(max_session))
        self.bind = bind or Bind()
        self.drain_timeout = drain_timeout
        self.ssl_context = ssl_context
        self.refresh_interval = float(refresh_interval)
        #: ``row, scheme -> TransportCredentials | None``; see
        #: :class:`AccessCredentials`.  ``None`` means this listener never
        #: authenticates to an upstream, which is the correct behaviour for a
        #: pool of open proxies.
        self.credentials = credentials
        if binding is not None:
            # What one listener serves.  The pool holds it, so there is exactly
            # one binding in force and no way for the GUI to store one that the
            # listener ignores.
            self.pool.default_binding = binding
        self.refresher = None
        # asyncio.Server.wait_closed() waits for listening sockets, not for
        # client handler tasks.  Track the latter so Background.close() can
        # cancel and await them before the helper loop is stopped.
        self.tasks = set()
        self.connections = {}
        self.streams = {}
        self.shutting_down = False
        self.closed_sessions = 0

    @property
    def binding(self):
        """The binding this listener serves right now."""
        return self.pool.default_binding

    def set_binding(self, binding):
        """Serve another pool/generation/profile, and say what that yields.

        The GUI stores a pool choice and used to keep serving the default pool,
        because the listener was built without it.  Applying it here makes the
        choice real at run time, and the report tells the caller how many rows
        the new binding can serve *right now* - a binding that pins a
        generation the current export does not carry serves nothing, and saying
        so is better than letting a client meet a 502 it cannot explain.
        """
        with self.pool.lock:
            self.pool.default_binding = binding if binding is not None else Binding()
            self.pool.cache.clear()
            rows = len(self.pool.matching(None, self.pool.default_binding))
        return dict(binding=self.pool.default_binding.as_dict(), rows=rows,
                    generation=self.pool.generation,
                    profile=(self.pool.status or {}).get('profile'))

    async def aset_binding(self, binding):
        """:meth:`set_binding` on the loop that owns the pool.

        The install itself is in-memory and instant; it is a coroutine so the
        caller can be sure it ran on the thread that serves the clients, which is
        what ``Background`` does.
        """
        return self.set_binding(binding)

    async def _credentials_for(self, proxy):
        """Resolve the credential of the row this proxy was chosen from.

        Off the event loop: the vault may be the OS keychain, and a locked
        keychain can take seconds.  The whole call is inside the caller's
        ``connect_timeout`` and the client's handshake deadline, so a slow store
        costs this one client and never the listener.
        """
        source = self.credentials
        if source is None:
            return None
        row = self.pool.row_for(proxy)
        if row is None:
            return None
        scheme = proxy.partition('://')[0]
        if scheme not in ('http', 'https', 'socks5', 'socks5h'):
            return None
        try:
            if inspect.iscoroutinefunction(source):
                return await source(row, scheme)
            return await asyncio.to_thread(source, row, scheme)
        except (secretstore.SecretError, OSError, ValueError, TypeError):
            # An unusable credential is an unusable upstream, never a listener
            # error: the client is told the same thing it is told for a dead
            # proxy, and the pool records the same verdict.
            return None

    # --- upstream selection --------------------------------------------------

    async def _tunnel(self, proxy, host, port, forward):
        credentials = await self._credentials_for(proxy)
        stream = await open_tunnel(proxy, host, port, forward, ssl_context=self.ssl_context,
                                   credentials=credentials)
        return stream, credentials

    async def connect(self, host, port, forward=False, request=None, session=None, binding=None,
                      sticky=None):
        """A tunnel through a working proxy, with the slot already reserved.

        The returned lease owns the concurrency slot; the caller must release it
        (or use ``with``) whatever happens to the stream.
        """
        tried = set()
        for attempt in range(self.attempts):
            lease = self.pool.reserve(request=request, exclude=tried, session=session,
                                      sticky=sticky, binding=binding)
            if lease is None:
                break
            proxy = lease.proxy
            tried.add(proxy)
            self.pool.stats['retries'] += attempt > 0
            started = time.monotonic()
            try:
                stream, credentials = await asyncio.wait_for(
                    self._tunnel(proxy, host, port, forward), self.connect_timeout)
            except (OSError, UpstreamError, ValueError, UnicodeError) as exc:
                lease.release()
                self.pool.outcome(proxy, 'handshake_failed', detail=UNUSABLE)
                if session and sticky == 'strict':
                    break
                continue
            except BaseException:
                # Cancellation and timeout both belong here: the slot must not leak.
                lease.release()
                raise
            # The plain-HTTP path still has to write the credential onto the
            # wire, so it rides on the lease and is dropped the moment the
            # request is written.
            lease.credentials = credentials
            self.pool.connected(proxy, (time.monotonic() - started) * 1000)
            return lease, stream
        self.pool.stats['failed'] += 1
        raise UpstreamError('NO_WORKING_PROXY' if tried else 'NO_PROXIES')

    # --- authentication ------------------------------------------------------

    def local_client(self, writer=None):
        """Whether a connection originated on this computer."""
        if writer is None:
            return False
        peer = writer.get_extra_info('peername')
        try:
            address = peer[0]
            return is_loopback(address) or address in local_interface_addresses()
        except (IndexError, TypeError):
            return False

    def password_ok(self, password, writer=None):
        if not self.token:
            return True
        # A LAN bind is authenticated for phones and other machines, while a
        # local browser/curl can keep the simple no-password loopback workflow.
        if self.allow_local_without_auth and self.local_client(writer):
            return True
        return hmac.compare_digest(password or b'', self.token.encode())

    @staticmethod
    def basic_credentials(headers):
        scheme, _, encoded = headers.get(b'proxy-authorization', b'').partition(b' ')
        if scheme.lower() != b'basic':
            return b'', b''
        try:
            user, _, password = base64.b64decode(encoded, validate=True).partition(b':')
        except ValueError:
            return b'', b''
        return user, password

    # --- relaying ------------------------------------------------------------

    @staticmethod
    def _discard(lease, upstream):
        """Give back a slot and close a tunnel that nobody will ever relay.

        Used on the path between a successful :meth:`connect` and the relay
        taking the lease.  Every exit from that window must end here, because
        the slot was already reserved and nobody else will release it.
        """
        with contextlib.suppress(OSError, RuntimeError):
            upstream[1].close()
        lease.release()

    async def relay(self, client_reader, client_writer, lease, upstream, first=b'', kind='tunnel'):
        """Carry one client exchange over one upstream, and report what it proved.

        ``first`` is the client's own request for the plain-HTTP path.  It is
        written once, to the lease it belongs to, after the tunnel is up: a
        request that reached an upstream is never sent to another one.
        """
        upstream_reader, upstream_writer = upstream
        proxy = lease.proxy
        state = {'ended': False, 'scored': False}
        transfers = []
        received = []
        task = asyncio.current_task()
        if task is not None:
            self.streams[task] = lease

        def score(kind):
            # One relayed exchange is scored once: the response head is the
            # verdict, and bytes that arrive later confirm it rather than
            # counting the same connection twice.
            if state['scored']:
                return
            state['scored'] = True
            self.pool.outcome(proxy, kind)

        def saw_bytes(total):
            if not received:
                received.append(total)
                score('tunnel_bytes')

        def sent_bytes(total):
            state['sent'] = True

        try:
            if first:
                guard = ReplayGuard()
                try:
                    guard.write_once(lease, upstream_writer, first)
                except UpstreamError:
                    self.pool.stats['replay_refused'] += 1
                    raise
                await upstream_writer.drain()
                # An upstream may wait for the body before producing any
                # response.  Upload concurrently, including when the client
                # waits for 100 Continue before sending its first body byte.
                transfers.append(asyncio.create_task(
                    pipe(client_reader, upstream_writer, self.idle_timeout,
                         on_bytes=sent_bytes, state=state)))
                status, head, reason = await response_head(upstream_reader, self.response_timeout)
                while status is not None and 100 <= status < 200 and status != 101:
                    client_writer.write(head)
                    await client_writer.drain()
                    # Only the final response supplies the health verdict.
                    status, head, reason = await response_head(upstream_reader, self.response_timeout)
                if reason == 'head' and status is None:
                    # Bytes arrived, but not an HTTP answer: whatever is at the
                    # other end of this proxy is not an HTTP proxy for us.
                    score('upstream_refused')
                elif reason == 'closed':
                    # The upstream took the request and closed without an
                    # answer.  That is a fault of the upstream, not of the
                    # target, and it is what the old TCP-open health missed.
                    score('upstream_refused')
                elif reason in ('timeout', 'oversized'):
                    # It hung.  Nothing is proven either way, so it is recorded
                    # as unknown instead of being blamed on the target.
                    score('no_response')
                elif status == 407:
                    # The upstream wants its own credentials.  Through this
                    # gateway it is unusable, and resting it is correct.
                    #
                    # The answer is relayed as the upstream sent it, which is
                    # this module's existing contract, and it leaks nothing about
                    # *our* secret: "no credential for this address" and "the
                    # credential we sent was rejected" produce the same bytes and
                    # the same pool verdict (see ``UNUSABLE``).  The one
                    # difference a client can see is against a proxy that is
                    # simply not reachable, and that is a property of the
                    # upstream address itself, which any observer could establish
                    # without asking this listener.  See the handoff note
                    # ``407`` for the stronger variant and what it would cost.
                    score('upstream_refused')
                elif status >= 500:
                    # The upstream answered and the target did not.  Resting the
                    # proxy would punish it for somebody else's outage.
                    score('upstream_unavailable')
                    self.pool.stats['upstream_5xx'] += 1
                else:
                    score('response')
                if head and status is not None:
                    client_writer.write(head)
                    await client_writer.drain()
                elif reason in ('head', 'closed', 'timeout', 'oversized'):
                    # The client asked for an HTTP request and got no usable
                    # answer.  Forwarding whatever arrived, or a bare disconnect,
                    # would look like a network glitch, so the gateway says what
                    # happened instead.
                    status_line = b'HTTP/1.1 504 Gateway Timeout\r\n' if reason == 'timeout' \
                        else b'HTTP/1.1 502 Bad Gateway\r\n'
                    body = tr('Выбранный прокси не ответил на запрос.',
                              'The chosen proxy did not answer the request.').encode()
                    with contextlib.suppress(OSError):
                        await self._refuse(client_writer, status_line, body)
                    # A generated 502/504 ends this exchange.  Never append a
                    # late upstream reply or keep uploading after that error.
                    return
            if not transfers:
                transfers.append(asyncio.create_task(
                    pipe(client_reader, upstream_writer, self.idle_timeout,
                         on_bytes=sent_bytes, state=state)))
            download = asyncio.create_task(pipe(upstream_reader, client_writer, self.idle_timeout,
                                                on_bytes=saw_bytes, state=state))
            transfers.append(download)
            # Forward requests ask the upstream to close after its response.
            # Its EOF therefore ends the exchange even if an upload is still
            # pending (for example after an early 413).  Tunnels retain their
            # independent half-close semantics in both directions.
            await asyncio.wait_for(download if first else asyncio.gather(*transfers),
                                   self.max_session or None)
            if not received and not first:
                # Nothing came back.  A client that opened a socket and left is
                # not evidence against the proxy, so only a client that really
                # sent something and got silence counts as a fault.
                if state.get('sent'):
                    score('closed_empty')
        except asyncio.TimeoutError:
            # Only the session cap can raise it here: pipe() swallows its own
            # idle timeouts, and response_head() classifies its own.  A capped
            # long connection is a policy decision, not a sign of idleness, so
            # the two are counted apart and visible in the snapshot.
            self.pool.stats['capped'] += 1
        except OSError:
            self.pool.stats['closed_idle'] += 1
        finally:
            for transfer in transfers:
                transfer.cancel()
            if task is not None:
                self.streams.pop(task, None)
            upstream_writer.close()
            # The credential has been on the wire or was never needed; either
            # way it leaves with the slot.
            lease.credentials = None
            lease.release()
            self.closed_sessions += 1
            # Release synchronously before another cancellation can interrupt
            # waiting for the child tasks to finish unwinding.
            await asyncio.gather(*transfers, return_exceptions=True)

    # --- client side ---------------------------------------------------------

    async def handle(self, reader, writer):
        self.pool.stats['connections'] += 1
        task = asyncio.current_task()
        if self.shutting_down:
            with contextlib.suppress(OSError, RuntimeError):
                writer.close()
                await writer.wait_closed()
            return
        if len(self.tasks) >= self.max_clients:
            # A bounded number of client connections; the rest are told why.
            self.pool.stats['rejected'] += 1
            with contextlib.suppress(OSError, RuntimeError):
                writer.write(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n')
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            return
        if task is not None:
            self.tasks.add(task)
            self.connections[task] = writer
        loop = asyncio.get_running_loop()
        # One absolute deadline for the whole handshake: the first byte, the
        # request line, the SOCKS5 negotiation and the upstream tunnel all have
        # to fit into it (defect 16, R11).  The relayed connection is not part
        # of the handshake and is governed by idle_timeout / max_session.
        deadline = loop.time() + self.handshake_timeout
        try:
            first = await asyncio.wait_for(reader.readexactly(1), _left(deadline))
            plan = await asyncio.wait_for(
                self.handle_socks5(reader, writer) if first == b'\x05'
                else self.handle_http(first, reader, writer), _left(deadline))
            if plan is not None:
                await self.relay(reader, writer, *plan)
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, UpstreamError, ValueError):
            self.pool.stats['closed_deadline'] += 1
        finally:
            with contextlib.suppress(OSError, RuntimeError):
                writer.close()
                await writer.wait_closed()
            if task is not None:
                self.connections.pop(task, None)
                self.tasks.discard(task)

    def _refuse(self, writer, code, body):
        writer.write(code + b'Content-Type: text/plain; charset=utf-8\r\n'
                     b'Content-Length: %d\r\n\r\n' % len(body) + body)
        return writer.drain()

    async def handle_http(self, first, reader, writer):
        head = first + await read_head(reader)
        lines = head[:-4].split(b'\r\n')
        method, target, version = (lines[0].split(b' ') + [b'', b''])[:3]
        headers = {}
        kept = []
        for line in lines[1:]:
            name, _, value = line.partition(b':')
            headers[name.strip().lower()] = value.strip()
            if name.strip().lower() not in HOP_HEADERS:
                kept.append(line)
        user, password = self.basic_credentials(headers)
        if not self.password_ok(password, writer):
            writer.write(b'HTTP/1.1 407 Proxy Authentication Required\r\n'
                         b'Proxy-Authenticate: Basic realm="proxy-workbench"\r\nContent-Length: 0\r\n\r\n')
            return await writer.drain()
        if method == b'GET' and target.split(b'?')[0] in (b'/', b'/status'):
            # Asked directly rather than as a proxy: show the pool state.
            body = json.dumps(await self.pool.asnapshot(20), indent=1).encode() + b'\n'
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nCache-Control: no-store\r\n'
                         b'Content-Length: %d\r\nConnection: close\r\n\r\n' % len(body) + body)
            return await writer.drain()
        try:
            request, session, pool_name = client_options(user.decode('utf-8', 'replace'))
            binding = self.pool.binding_for(pool_name)
            if method == b'CONNECT':
                host, port = split_target(target.decode('ascii'), 443)
                path = None
            else:
                url = target.decode('ascii')
                if not url.lower().startswith('http://'):
                    raise ValueError('absolute http:// URL expected')
                authority, _, rest = url[7:].partition('/')
                host, port = split_target(authority, 80)
                path = '/' + rest
                absolute = url
        except (ValueError, UnicodeError, KeyError):
            writer.write(b'HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n')
            return await writer.drain()
        sticky = 'strict' if (session and binding.option('sticky') == 'strict') else None
        try:
            lease, upstream = await self.connect(host, port, forward=path is not None,
                                                 request=request, session=session, binding=binding,
                                                 sticky=sticky)
        except UpstreamError as exc:
            body = tr('Нет рабочих прокси: запустите проверку.', 'No working proxies: run a check first.') \
                if str(exc) == 'NO_PROXIES' else tr('Все выбранные прокси не ответили.',
                                                    'None of the tried proxies answered.')
            return await self._refuse(writer, b'HTTP/1.1 502 Bad Gateway\r\n', body.encode())
        # The slot is ours from here until the relay takes it.  Telling the
        # client "connection established" is still an await, and a shutdown, a
        # handshake deadline or a client that hangs up in exactly that window
        # must not leave the slot taken (defect 16).
        try:
            if path is None:
                writer.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
                await writer.drain()
                return lease, upstream, b'', 'tunnel'
            # Plain HTTP: one request per connection keeps rotation simple and
            # predictable.  The request is written exactly once, after the tunnel
            # is up, and only to the upstream that owns this lease.
            target = absolute if lease.proxy.startswith(('http://', 'https://')) else path
            # The client's own Proxy-Authorization was stripped above with the
            # hop headers: it authenticates *this* listener, and the upstream
            # gets the credential of the access this row was measured with, or
            # none at all.
            extra = [header.encode('ascii') for header in
                     filter(None, (authorization_header(lease.credentials),))]
            payload = b'\r\n'.join([b' '.join([method, target.encode('ascii'), version or b'HTTP/1.1']),
                                    *kept, *extra, b'Connection: close']) + b'\r\n\r\n'
            return lease, upstream, payload, 'forward'
        except BaseException:
            self._discard(lease, upstream)
            raise

    async def handle_socks5(self, reader, writer):
        methods = await reader.readexactly((await reader.readexactly(1))[0])
        # User/password is required for a remote token client and preferred
        # otherwise: the user name carries options. A loopback client may use
        # the no-auth method even when the same listener is LAN-enabled.
        wanted = 2 if (self.token and not (self.allow_local_without_auth and self.local_client(writer))) \
            or 2 in methods else 0
        if wanted not in methods:
            writer.write(b'\x05\xff')
            return await writer.drain()
        writer.write(bytes([5, wanted]))
        await writer.drain()
        user = b''
        if wanted == 2:
            await reader.readexactly(1)
            user = await reader.readexactly((await reader.readexactly(1))[0])
            password = await reader.readexactly((await reader.readexactly(1))[0])
            granted = self.password_ok(password, writer)
            writer.write(b'\x01\x00' if granted else b'\x01\x01')
            await writer.drain()
            if not granted:
                return
        version, command, _, kind = await reader.readexactly(4)
        if kind == 1:
            host = ipaddress.IPv4Address(await reader.readexactly(4)).compressed
        elif kind == 4:
            host = ipaddress.IPv6Address(await reader.readexactly(16)).compressed
        elif kind == 3:
            host = (await reader.readexactly((await reader.readexactly(1))[0])).decode('idna')
        else:
            host = None
        port = struct.unpack('>H', await reader.readexactly(2))[0]
        if version != 5 or command != 1 or host is None:
            writer.write(b'\x05\x07\x00\x01' + bytes(6))
            return await writer.drain()
        try:
            request, session, pool_name = client_options(user.decode('utf-8', 'replace'))
            binding = self.pool.binding_for(pool_name)
            lease, upstream = await self.connect(host, port, request=request, session=session,
                                                 binding=binding)
        except (UpstreamError, ValueError, KeyError):
            writer.write(b'\x05\x01\x00\x01' + bytes(6))
            return await writer.drain()
        # Same rule as the HTTP path: the grant is an await, so a cancellation
        # or a client that hangs up while it is written must give the slot back
        # rather than leak it (defect 16).
        try:
            writer.write(b'\x05\x00\x00\x01' + bytes(6))
            await writer.drain()
        except BaseException:
            self._discard(lease, upstream)
            raise
        return lease, upstream, b'', 'tunnel'

    # --- lifecycle -----------------------------------------------------------

    def set_denylist(self, denylist):
        """Install a denylist and, under ``on_deny='close'``, drop denied streams.

        The policy lives in one place, :meth:`Pool.revoke_streams`, so what is
        revoked from the pool and what is cut on the wire can never disagree.
        ``keep`` is the default and it is a real decision, not an omission: an
        open stream belongs to a client that already has bytes on the wire, and
        cutting it is the user's call, not a side effect of adding a rule.
        """
        self.pool.set_denylist(denylist)
        denied = set(self.pool.revoke_streams())
        if not denied:
            return []
        closed = []
        for task, lease in list(self.streams.items()):
            if lease.proxy in denied and not task.done():
                writer = self.connections.get(task)
                if writer is not None:
                    with contextlib.suppress(OSError, RuntimeError):
                        writer.close()
                task.cancel()
                closed.append(lease.proxy)
        return closed

    async def shutdown(self, grace=None):
        """Stop serving and end every client connection with a bounded wait."""
        self.shutting_down = True
        refresher, self.refresher = self.refresher, None
        if refresher is not None and not refresher.done():
            refresher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresher
        grace = self.drain_timeout if grace is None else grace
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, grace)
        while self.tasks and loop.time() < deadline:
            await asyncio.sleep(0.01)
        pending = [task for task in self.tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        return dict(drained=len(self.tasks) - len(pending), forced=len(pending),
                    closed_sessions=self.closed_sessions)

    def state(self):
        """Visible state of the listener: bind, identity and policy, no secrets."""
        return dict(self.pool.state(), bind=self.bind.as_dict(),
                    listen_address=self.listen_address(),
                    token_origin=self.token_origin, authenticated=bool(self.token),
                    lan=self.bind.lan, reachable_from_lan=self.reachable_from_lan,
                    interfaces=lan_interfaces(),
                    # Whether upstream authentication is even possible for this
                    # listener, and how often a stored credential was not
                    # usable.  Deliberately NOT part of ``snapshot()``, which the
                    # listener serves to anybody who asks; ``state()`` is the
                    # local operator's view.  No value is ever published.
                    credentials=credential_state(self.credentials),
                    handshake_timeout=self.handshake_timeout, connect_timeout=self.connect_timeout,
                    idle_timeout=self.idle_timeout, max_session=self.max_session,
                    max_clients=self.max_clients, clients=len(self.tasks),
                    refresh_interval=self.refresh_interval,
                    closing=self.shutting_down, supported=list(SUPPORTED),
                    strategies=list(STRATEGIES), sticky_modes=list(STICKY_MODES),
                    revoke_policies=list(REVOKE_POLICIES))

    def listen_address(self):
        """``host:port`` the socket really bound, brackets for IPv6."""
        host = self.bind.listen_host
        return f'[{host}]:{self.bind.port}' if ':' in host else f'{host}:{self.bind.port}'

    @property
    def reachable_from_lan(self):
        """Whether another machine on this network can actually use the listener.

        F17 asks for a visible difference between a local proxy and a phone
        connection.  All three conditions are required: the bind is not
        loopback, LAN was asked for, and a password exists - a listener that
        other machines can reach without one is not a phone connection, it is
        an open relay.
        """
        return bool(self.bind.lan and not self.bind.local and self.token)

    async def refresh_loop(self, interval=None):
        """Follow the current export without reading a file on the event loop.

        The rows a client may be served are pinned to a binding; this loop only
        decides *when* the listener notices that the export or the denylist
        moved, so the wait is a visible interval instead of a hidden block in
        the middle of somebody's request.
        """
        interval = self.refresh_interval if interval is None else interval
        while not self.shutting_down:
            await asyncio.sleep(interval)
            try:
                await self.pool.arefresh()
            except (OSError, ValueError, KeyError, TypeError):
                continue


def _left(deadline):
    """Remaining seconds until an absolute deadline, for ``asyncio.wait_for``."""
    left = deadline - asyncio.get_running_loop().time()
    if left <= 0:
        raise asyncio.TimeoutError
    return left


def resolve_bind(host, port, lan=None, bind=None, interface=None):
    """One bind out of the ways a caller may ask for it.

    ``bind=`` wins and ``lan=`` must agree with it, so a contradictory pair is
    refused rather than half-applied.  Without either, LAN is off and the
    listener stays on loopback.  ``interface`` chooses which adapter a LAN
    listener binds and publishes; it is meaningless without the opt-in, so it is
    refused rather than quietly ignored.
    """
    if interface is not None and not (lan or (bind is not None and bind.lan)):
        raise ValueError(tr('Интерфейс LAN требует явного включения LAN (lan=True).',
                            'a LAN interface requires the explicit opt-in (lan=True).'))
    if bind is None:
        return Bind(host=host, port=port, lan=bool(lan) if lan is not None else False,
                    interface=interface)
    if lan is not None and bool(lan) != bind.lan:
        raise ValueError(tr('bind.lan противоречит аргументу lan.', 'bind.lan contradicts the lan argument.'))
    if interface is not None and interface != bind.interface:
        raise ValueError(tr('Интерфейс противоречит bind.interface.',
                            'interface contradicts bind.interface.'))
    return bind


def listener_binding(binding, default_binding=None):
    """The one binding a listener serves, or a refusal when two disagree.

    A GUI stores a pool choice and a caller may also pass the pool's own
    default.  Silently preferring one of them is how a listener ends up serving
    a pool the user did not pick, so two different answers are an error.
    """
    if binding is None:
        return default_binding
    if default_binding is not None and default_binding.as_dict() != binding.as_dict():
        raise ValueError(tr('Указаны разные привязки шлюза.', 'Two different gateway bindings were given.'))
    return binding


async def start(data, host=DEFAULT_HOST, port=DEFAULT_PORT, token=None, filters=None,
                strategy='round-robin', max_per_proxy=0, session_ttl=600, *, bind=None, lan=None,
                interface=None, sticky='failover', denylist=None, denylist_path=None, denylist_normalizer=None,
                on_deny='keep', bindings=None, default_binding=None, binding=None,
                credentials=None,
                handshake_timeout=30, max_clients=MAX_CLIENTS,
                max_session=0, cache_limit=CACHE_LIMIT, attempts=3, connect_timeout=8,
                idle_timeout=300, refresh_interval=2.0, response_timeout=15,
                max_failures=2, cooldown=300):
    """Listen for proxy clients and spread their connections over the export.

    A non-loopback address needs ``lan=True`` (or ``bind=Bind(lan=True)``) and its
    own password.  When none is given, a dedicated one is generated here - never
    the GUI session token and never the API token.  With ``lan=True`` and a
    loopback address the listener binds the wildcard instead, because asking for
    LAN and getting a loopback socket back is the bug this closes.

    ``binding`` is the pool, generation, profile and policy this listener
    serves; ``credentials`` resolves a chosen row's access identity into the
    bytes the upstream handshake needs (see :class:`AccessCredentials`).
    """
    bind = resolve_bind(host, port, lan, bind, interface)
    origin = 'none'
    token, origin = resolve_token(bind, token)
    default_binding = listener_binding(binding, default_binding)
    pool = Pool(data, filters, strategy, max_per_proxy=max_per_proxy, session_ttl=session_ttl,
                sticky=sticky, denylist=denylist, denylist_path=denylist_path,
                denylist_normalizer=denylist_normalizer, on_deny=on_deny,
                cache_limit=cache_limit, bindings=bindings, default_binding=default_binding,
                max_failures=max_failures, cooldown=cooldown)
    gateway = Gateway(pool, token, attempts=attempts, connect_timeout=connect_timeout,
                      idle_timeout=idle_timeout,
                      # The no-password exception exists only where the listener
                      # really is reachable from the network; on loopback a
                      # configured password is required from every client.
                      allow_local_without_auth=bind.lan and not bind.local,
                      handshake_timeout=handshake_timeout, max_clients=max_clients,
                      max_session=max_session, bind=bind, token_origin=origin,
                      refresh_interval=refresh_interval, response_timeout=response_timeout,
                      credentials=credentials)
    # The first read happens before the listener opens, off the loop, so the very
    # first client already sees a real generation.
    await pool.arefresh()
    server = await asyncio.start_server(gateway.handle, bind.listen_host, bind.port, limit=MAX_HEAD)
    server.gateway = gateway
    server.bind = bind
    gateway.refresher = asyncio.get_running_loop().create_task(gateway.refresh_loop())
    return server


class Background:
    """The gateway on its own event loop thread, for the GUI."""

    def __init__(self, data, host=DEFAULT_HOST, port=DEFAULT_PORT, token=None, **options):
        self.loop = asyncio.new_event_loop()
        bind = resolve_bind(host, port, options.pop('lan', None), options.pop('bind', None),
                            options.pop('interface', None))
        self.bind = bind
        self.server = self.loop.run_until_complete(start(data, bind=bind, token=token, **options))
        self.host = bind.host
        self.listen_host = bind.listen_host
        self.display_host = bind.published_host
        self.token = self.server.gateway.token
        self.token_origin = self.server.gateway.token_origin
        self.lan = bind.lan
        self.port = self.server.sockets[0].getsockname()[1]
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self._closed = False
        self.shutdown_report = None

    @property
    def gateway(self):
        return self.server.gateway

    def state(self):
        """The same visible state the GUI and the API show; no secret values."""
        return dict(self.server.gateway.state(), port=self.port, interfaces=lan_interfaces())

    def set_binding(self, binding):
        """Apply a stored pool choice to the running listener.

        The GUI validates and stores a binding in ``POST /api/gateway/config``
        and then rebuilds the listener; this is the path for a caller that would
        rather not restart, and it reports the same way a fresh listener would:
        how many rows the new binding can serve right now.
        """
        if self._closed:
            raise ValueError('the gateway is closed')
        return asyncio.run_coroutine_threadsafe(
            self.server.gateway.aset_binding(binding), self.loop).result(5)

    def close(self):
        if self._closed:
            return
        gateway = self.server.gateway

        async def stop():
            # Drain accept callbacks that were queued before close while the
            # listening socket is still valid.  Closing first can make a
            # callback create a transport against an already-detached server.
            for _ in range(4):
                await asyncio.sleep(0)
            self.server.close()
            for _ in range(2):
                await asyncio.sleep(0)
            return await gateway.shutdown()

        stopped = False
        try:
            self.shutdown_report = asyncio.run_coroutine_threadsafe(stop(), self.loop).result(5)
            stopped = True
        except (TimeoutError, RuntimeError):
            # A misbehaving client must not make us close an event loop with
            # live tasks.  The loop/thread remain available for a later retry;
            # normal handler cancellation completes within the bounded wait.
            pass
        if stopped and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        if stopped and not self.thread.is_alive():
            if not self.loop.is_closed():
                self.loop.close()
            self._closed = True
