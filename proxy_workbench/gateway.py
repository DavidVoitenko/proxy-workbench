"""Rotating proxy gateway: one local address that spreads connections over working proxies.

Browsers, scripts and apps point at ``127.0.0.1:8899`` as an HTTP or SOCKS5 proxy.
Every new connection goes out through the next proxy from the latest export;
a proxy that fails is skipped and rested for a while, and the connection is
retried through another one before the client sees an error.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import ipaddress
import random
import socket
import struct
import time
from pathlib import Path

from . import socks4
from .api import Exports, is_loopback, select
from .i18n import tr

DEFAULT_PORT = 8899
SUPPORTED = ('http', 'socks4', 'socks5')
STRATEGIES = ('round-robin', 'random')
MAX_HEAD = 64 * 1024
HOP_HEADERS = {b'proxy-authorization', b'proxy-connection', b'connection', b'keep-alive'}


class UpstreamError(Exception):
    pass


class Pool:
    """Working proxies from the latest export with rotation and failure cool-down."""

    def __init__(self, data, filters=None, strategy='round-robin', max_failures=2, cooldown=300):
        self.exports = Exports(Path(data) / 'exports')
        self.filters = {'protocol': 'all', 'countries': (), 'anonymity': 'any', 'max_latency': 0, **(filters or {})}
        self.strategy = strategy
        self.max_failures = max_failures
        self.cooldown = cooldown
        self.failures = {}
        self.resting = {}
        self.position = 0
        self.key = None
        self.proxies = []
        self.stats = dict(connections=0, failed=0, retries=0)

    def refresh(self):
        rows, _ = self.exports.load()
        if self.exports.key != self.key:
            self.key = self.exports.key
            self.proxies = [row['proxy'] for row in select(rows, self.filters)
                            if row['protocol'] in SUPPORTED and not row['proxy'].startswith('https://')]
        return self.proxies

    def available(self, now=None):
        now = time.monotonic() if now is None else now
        return [proxy for proxy in self.refresh() if self.resting.get(proxy, 0) <= now]

    def pick(self, exclude=()):
        candidates = [proxy for proxy in self.available() if proxy not in exclude]
        if not candidates:
            return None
        if self.strategy == 'random':
            return random.choice(candidates)
        self.position = (self.position + 1) % len(candidates)
        return candidates[self.position]

    def ok(self, proxy):
        self.failures.pop(proxy, None)

    def failed(self, proxy):
        self.failures[proxy] = self.failures.get(proxy, 0) + 1
        if self.failures[proxy] >= self.max_failures:
            self.resting[proxy] = time.monotonic() + self.cooldown
            self.failures.pop(proxy)


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


async def open_tunnel(proxy, host, port, forward=False):
    """A stream to host:port through `proxy`, after the proxy's own handshake.

    With ``forward`` an HTTP proxy gets the plain request itself instead of a
    CONNECT tunnel, since many HTTP proxies allow CONNECT only to port 443.
    """
    scheme, _, address = proxy.partition('://')
    proxy_host, _, proxy_port = address.rpartition(':')
    reader, writer = await asyncio.open_connection(proxy_host.strip('[]'), int(proxy_port), limit=MAX_HEAD)
    try:
        if scheme == 'http' and forward:
            pass
        elif scheme == 'http':
            target = f'[{host}]:{port}' if ':' in host else f'{host}:{port}'
            writer.write(f'CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n'.encode())
            await writer.drain()
            status = (await read_head(reader)).split(b'\r\n', 1)[0].split()
            if len(status) < 2 or status[1] != b'200':
                raise UpstreamError('CONNECT_REFUSED')
        elif scheme == 'socks4':
            ip = await resolve(host, port, socket.AF_INET)
            writer.write(socks4.connect_request(ipaddress.IPv4Address(ip).packed, port))
            await writer.drain()
            try:
                socks4.check_reply(await read_exactly(reader, socks4.REPLY_SIZE))
            except socks4.Socks4Error as exc:
                raise UpstreamError(str(exc)) from None
        elif scheme in ('socks5', 'socks5h'):
            writer.write(b'\x05\x01\x00')
            await writer.drain()
            if await read_exactly(reader, 2) != b'\x05\x00':
                raise UpstreamError('SOCKS5_AUTH')
            if scheme == 'socks5':
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
            if reply[1] != 0:
                raise UpstreamError(f'SOCKS5_REJECTED_{reply[1]}')
            skip = {1: 4, 4: 16}.get(reply[3])
            if skip is None:
                skip = (await read_exactly(reader, 1))[0]
            await read_exactly(reader, skip + 2)
        else:
            raise UpstreamError('UNSUPPORTED')
    except BaseException:
        writer.close()
        raise
    return reader, writer


async def pipe(reader, writer, idle):
    try:
        while True:
            data = await asyncio.wait_for(reader.read(65536), idle)
            if not data:
                break
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (OSError, asyncio.TimeoutError):
        writer.close()


def split_target(value, default_port):
    host, sep, port = value.rpartition(':')
    if not sep or not port.isdigit() or ']' in port:
        host, port = value, default_port
    host = host.strip('[]')
    if not host or not 0 < int(port) < 65536:
        raise ValueError('bad target')
    return host, int(port)


class Gateway:
    def __init__(self, pool, token=None, attempts=3, connect_timeout=8, idle_timeout=300):
        self.pool = pool
        self.token = token
        self.attempts = attempts
        self.connect_timeout = connect_timeout
        self.idle_timeout = idle_timeout

    async def connect(self, host, port, forward=False):
        tried = []
        for attempt in range(self.attempts):
            proxy = self.pool.pick(exclude=tried)
            if proxy is None:
                break
            tried.append(proxy)
            self.pool.stats['retries'] += attempt > 0
            try:
                stream = await asyncio.wait_for(open_tunnel(proxy, host, port, forward), self.connect_timeout)
            except (OSError, UpstreamError, asyncio.TimeoutError, ValueError, UnicodeError):
                self.pool.failed(proxy)
                continue
            self.pool.ok(proxy)
            return proxy, stream
        self.pool.stats['failed'] += 1
        raise UpstreamError('NO_WORKING_PROXY' if tried else 'NO_PROXIES')

    def authorized(self, headers):
        if not self.token:
            return True
        value = headers.get(b'proxy-authorization', b'')
        scheme, _, encoded = value.partition(b' ')
        if scheme.lower() != b'basic':
            return False
        try:
            password = base64.b64decode(encoded, validate=True).partition(b':')[2]
        except ValueError:
            return False
        return hmac.compare_digest(password, self.token.encode())

    async def relay(self, client_reader, client_writer, upstream, first=b''):
        upstream_reader, upstream_writer = upstream
        if first:
            upstream_writer.write(first)
        try:
            await asyncio.gather(pipe(client_reader, upstream_writer, self.idle_timeout),
                                 pipe(upstream_reader, client_writer, self.idle_timeout))
        finally:
            upstream_writer.close()

    async def handle(self, reader, writer):
        self.pool.stats['connections'] += 1
        try:
            first = await asyncio.wait_for(reader.readexactly(1), 30)
            if first == b'\x05':
                await self.handle_socks5(reader, writer)
            else:
                await self.handle_http(first, reader, writer)
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, UpstreamError, ValueError):
            pass
        finally:
            writer.close()

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
        if not self.authorized(headers):
            writer.write(b'HTTP/1.1 407 Proxy Authentication Required\r\n'
                         b'Proxy-Authenticate: Basic realm="proxy-workbench"\r\nContent-Length: 0\r\n\r\n')
            return await writer.drain()
        try:
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
        except (ValueError, UnicodeError):
            writer.write(b'HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n')
            return await writer.drain()
        try:
            proxy, upstream = await self.connect(host, port, forward=path is not None)
        except UpstreamError as exc:
            message = tr('Нет рабочих прокси: запустите проверку.', 'No working proxies: run a check first.') \
                if str(exc) == 'NO_PROXIES' else tr('Все выбранные прокси не ответили.', 'None of the tried proxies answered.')
            body = message.encode()
            writer.write(b'HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain; charset=utf-8\r\n'
                         b'Content-Length: %d\r\n\r\n' % len(body) + body)
            return await writer.drain()
        if path is None:
            writer.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
            await writer.drain()
            return await self.relay(reader, writer, upstream)
        # Plain HTTP: one request per connection keeps rotation simple and predictable.
        target = absolute if proxy.startswith('http://') else path
        request = b'\r\n'.join([b' '.join([method, target.encode('ascii'), version or b'HTTP/1.1']), *kept,
                                b'Connection: close']) + b'\r\n\r\n'
        await self.relay(reader, writer, upstream, request)

    async def handle_socks5(self, reader, writer):
        methods = await reader.readexactly((await reader.readexactly(1))[0])
        wanted = 2 if self.token else 0
        if wanted not in methods:
            writer.write(b'\x05\xff')
            return await writer.drain()
        writer.write(bytes([5, wanted]))
        await writer.drain()
        if wanted == 2:
            await reader.readexactly(1)
            await reader.readexactly((await reader.readexactly(1))[0])
            password = await reader.readexactly((await reader.readexactly(1))[0])
            granted = hmac.compare_digest(password, self.token.encode())
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
            _, upstream = await self.connect(host, port)
        except UpstreamError:
            writer.write(b'\x05\x01\x00\x01' + bytes(6))
            return await writer.drain()
        writer.write(b'\x05\x00\x00\x01' + bytes(6))
        await writer.drain()
        await self.relay(reader, writer, upstream)


async def start(data, host='127.0.0.1', port=DEFAULT_PORT, token=None, filters=None, strategy='round-robin'):
    if not is_loopback(host) and not token:
        raise ValueError(tr(f'Шлюз на {host} доступен из сети: задайте пароль через --api-token.',
                            f'the gateway on {host} is reachable from the network: set a password with --api-token'))
    gateway = Gateway(Pool(data, filters, strategy), token)
    server = await asyncio.start_server(gateway.handle, host, port, limit=MAX_HEAD)
    server.gateway = gateway
    return server


class Background:
    """The gateway on its own event loop thread, for the GUI."""

    def __init__(self, data, host='127.0.0.1', port=DEFAULT_PORT):
        import threading
        self.loop = asyncio.new_event_loop()
        self.server = self.loop.run_until_complete(start(data, host, port))
        self.port = self.server.sockets[0].getsockname()[1]
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def close(self):
        async def stop():
            self.server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.server.wait_closed(), 2)
        asyncio.run_coroutine_threadsafe(stop(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
