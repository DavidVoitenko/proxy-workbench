"""Local upstreams and exports for the gateway tests.

Everything here runs on loopback inside the test process: no public proxy, no
DNSBL, no third-party service is contacted.  A "proxy" is an asyncio server
that speaks one upstream protocol and records what it was asked to do, so a
test can assert on the bytes the gateway actually produced.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import json
import socket
import struct
import tempfile
import time
import unittest
from pathlib import Path

import httpx

from proxy_workbench import gateway


async def shutdown(writer):
    writer.close()
    with contextlib.suppress(OSError, RuntimeError):
        await writer.wait_closed()


async def splice(a_reader, a_writer, b_reader, b_writer):
    async def one(reader, writer):
        with contextlib.suppress(OSError):
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
            if writer.can_write_eof():
                writer.write_eof()
    await asyncio.gather(one(a_reader, b_writer), one(b_reader, a_writer))
    await shutdown(a_writer)
    await shutdown(b_writer)


def export_row(proxy, country=None, latency=100, valid_for=3600, **extra):
    now = time.time()
    return dict(proxy=proxy, country=country, reliability=1, min_target_reliability=1,
                latency_ms=latency, jitter_ms=1, score=90, successes=3, requests=3,
                checked_at=now, valid_until=now + valid_for, samples=[], **extra)


def write_export(home, proxies, *, generation='.generation-aaaaaaaa', profile='p1',
                 profile_revision=1, country=None, rows=None):
    """Publish an immutable generation and point current.json at it."""
    exports = Path(home) / 'exports'
    rows = list(rows) if rows is not None else [export_row(proxy, country=country) for proxy in proxies]
    if generation is None:
        exports.mkdir(parents=True, exist_ok=True)
        (exports / 'ranked.json').write_text(json.dumps(rows), encoding='utf-8')
        return
    target = exports / 'generations' / generation
    target.mkdir(parents=True, exist_ok=True)
    (target / 'ranked.json').write_text(json.dumps(rows), encoding='utf-8')
    (target / 'status.json').write_text(json.dumps({
        'schema_version': 1, 'generation': generation, 'state': 'complete',
        'stop_reason': 'complete', 'complete': True, 'checked': len(rows),
        'scope_candidates': len(rows), 'exported': len(rows), 'profile': profile,
        'profile_revision': profile_revision,
        'valid_until': min((row['valid_until'] for row in rows), default=time.time() + 3600),
    }), encoding='utf-8')
    (exports / 'current.json').write_text(json.dumps({
        'generation': generation, 'files': ['ranked.json', 'status.json'], 'state': 'complete',
    }), encoding='utf-8')


class Upstream:
    """A fake upstream proxy: a listener plus everything it was asked to do."""

    def __init__(self, scheme, host='127.0.0.1'):
        self.scheme = scheme
        self.host = host
        self.port = None
        self.connections = 0
        self.open = 0
        self.peak_open = 0
        self.requests = []
        self.targets = []
        self.mode = None

    @property
    def url(self):
        return f'{self.scheme}://{self.host}:{self.port}'

    def __repr__(self):
        return f'<{self.scheme} {self.host}:{self.port} conns={self.connections}>'


class ScriptedClient:
    """A client side whose bytes and writes the test controls exactly.

    A real client can only be *raced*: the gateway decides for itself when to
    read and when to write, so a test that wants to cancel or expire it inside
    one particular ``await`` - the one where the tunnel grant is flushed -
    would have to guess.  Here ``drain`` can be held open from the moment a
    chosen marker is written, which pins the gateway at that await until the
    test cancels it.  That window is exactly the one a sequential
    pick/acquire test cannot produce, and exactly the one where a reserved
    slot used to be lost.
    """

    def __init__(self, script=b'', block_on=b''):
        self.script = bytearray(script)
        self.buf = b''
        self.block_on = block_on
        self.held = asyncio.Event()
        self.closed = False

    async def _await_more(self):
        """Block forever, the way a client that stops talking does."""
        await asyncio.Future()

    async def readexactly(self, size):
        while len(self.script) < size:
            await self._await_more()
        out = bytes(self.script[:size])
        del self.script[:size]
        return out

    async def readuntil(self, separator):
        while separator not in self.script:
            await self._await_more()
        end = self.script.index(separator) + len(separator)
        out = bytes(self.script[:end])
        del self.script[:end]
        return out

    async def read(self, size):
        return b''

    def write(self, data):
        self.buf += data
        if self.block_on and self.block_on in self.buf:
            self.held.set()

    async def drain(self):
        if self.held.is_set():
            await self._await_more()

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass

    def can_write_eof(self):
        return False

    def write_eof(self):
        pass

    def get_extra_info(self, name, default=None):
        return default


class GatewayCase(unittest.IsolatedAsyncioTestCase):
    """A loopback export, local upstreams and a gateway, all in one temporary home."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.servers = []
        self.ups = []
        self.target = await self.target_service()
        self.target6 = None
        if socket.has_ipv6:
            with contextlib.suppress(OSError):
                self.target6 = await self.target_service_v6()

    async def asyncTearDown(self):
        # Server.wait_closed() waits for live handler tasks, so a test that
        # deliberately leaves a client suspended would stall the whole class.
        for server in self.servers:
            running = getattr(server, 'gateway', None)
            if running is not None:
                with contextlib.suppress(Exception):
                    await running.shutdown(0)
        for server in self.servers:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        self.temp.cleanup()

    # --- plumbing ----------------------------------------------------------

    async def listen(self, handler, host='127.0.0.1', ssl=None):
        server = await asyncio.start_server(handler, host, 0, ssl=ssl)
        self.servers.append(server)
        return server.sockets[0].getsockname()[1]

    def publish(self, proxies, **options):
        write_export(self.home, proxies, **options)
    def deny(self, *values):
        """Write data/denylist.txt the way the GUI writes it."""
        (self.home / 'denylist.txt').write_text('\n'.join(values) + '\n', encoding='utf-8')

    @staticmethod
    def local_normalizer(value):
        """A canonicaliser for the loopback fixtures, standing in for core.normalize.

        core.normalize refuses private addresses, and every upstream in a test is
        on 127.0.0.1, so the rules are written as full proxy URLs instead.
        """
        return value.strip().lower() if '://' in value else None

    async def start(self, **options):
        options.setdefault('port', 0)
        server = await gateway.start(self.home, **options)
        self.servers.append(server)
        self.addAsyncCleanup(self._stop, server)
        return server, f'127.0.0.1:{server.sockets[0].getsockname()[1]}'

    async def _stop(self, server):
        with contextlib.suppress(Exception):
            await server.gateway.shutdown(0)
        server.close()
        with contextlib.suppress(Exception):
            await server.wait_closed()

    async def wait_for(self, predicate, timeout=5, interval=0.01):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if predicate():
                return True
            await asyncio.sleep(interval)
        return bool(predicate())

    # --- the target the client actually wants ------------------------------

    async def target_service_v6(self, body=b'local target v6'):
        """The same target on ::1, for the IPv6 transport checks."""
        handler = None

        async def serve(reader, writer):
            try:
                head = await reader.readuntil(b'\r\n\r\n')
                payload = body + b' for ' + head.split(b' ')[1]
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n'
                             % len(payload) + payload)
                await writer.drain()
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError):
                pass
            finally:
                await shutdown(writer)
        return await self.listen(serve, host='::1')

    async def target_service(self, body=b'local target'):
        async def handler(reader, writer):
            try:
                head = await reader.readuntil(b'\r\n\r\n')
                payload = body + b' for ' + head.split(b' ')[1]
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n'
                             % len(payload) + payload)
                await writer.drain()
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError):
                pass  # a test may hang up at any moment
            finally:
                await shutdown(writer)
        return await self.listen(handler)

    # --- upstreams ---------------------------------------------------------

    def _track(self, up, writer, on_close=None):
        up.connections += 1
        up.open += 1
        up.peak_open = max(up.peak_open, up.open)
        original = writer.close

        def counted(*args, **kwargs):
            up.open = max(0, up.open - 1)
            if on_close is not None:
                on_close()
            return original(*args, **kwargs)
        writer.close = counted
        return writer

    async def socks_upstream(self, scheme='socks5', stall=False, delay=0.0):
        """A SOCKS4/4a/5/5h upstream that records how the name was carried."""
        up = Upstream(scheme)

        async def handler(reader, writer):
            self._track(up, writer)
            try:
                await self._serve_socks(reader, writer, scheme, stall, delay, up)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError):
                pass  # a test may hang up at any moment
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a broken fake must not print a traceback
                pass
            finally:
                await shutdown(writer)

        up.port = await self.listen(handler)
        self.ups.append(up)
        return up

    async def _serve_socks(self, reader, writer, scheme, stall, delay, up):
        if stall:
            # Accept, read whatever the gateway says and never answer, until the
            # gateway hangs up.  The gateway therefore stays suspended inside
            # its own handshake, which is the interleaving a sequential
            # pick/acquire test cannot produce.
            with contextlib.suppress(asyncio.TimeoutError, OSError):
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and await reader.read(65536):
                    pass
            return
        if delay:
            await asyncio.sleep(delay)
        host, port = await self._read_socks_request(reader, writer, scheme, up)
        up.targets.append(dict(host=host, port=port, mode=up.mode, via=scheme))
        upstream = await asyncio.open_connection(host, port)
        await splice(reader, writer, *upstream)

        up.port = await self.listen(handler)
        self.ups.append(up)
        return up

    async def _read_socks_request(self, reader, writer, scheme, up=None):
        """Consume one SOCKS handshake, answer it, and return (host, port).

        ``up.mode`` records how the name travelled, which is the whole point of
        the socks4/socks4a/socks5/socks5h comparison.
        """
        if scheme.startswith('socks4'):
            head = await reader.readexactly(8)
            port = struct.unpack('>H', head[2:4])[0]
            address = head[4:8]
            # SOCKS4a adds a second NUL-terminated field AFTER USERID.
            up.user_id = (await reader.readuntil(b'\x00'))[:-1]
            if address[:3] == b'\x00\x00\x00' and address[3] != 0:
                name = (await reader.readuntil(b'\x00'))[:-1]
                up.mode = 'socks4a-name'
                host = name.decode('idna')
            else:
                up.mode = 'socks4-ipv4'
                host = ipaddress.IPv4Address(address).compressed
            writer.write(b'\x00\x5a' + b'\x00' * 6)
            await writer.drain()
            return host, port
        greeting = await reader.readexactly(2)
        await reader.readexactly(greeting[1])
        writer.write(b'\x05\x00')
        await writer.drain()
        _version, _command, _, kind = await reader.readexactly(4)
        if kind == 1:
            host, mode = ipaddress.IPv4Address(await reader.readexactly(4)).compressed, 'name-resolved-v4'
        elif kind == 4:
            host, mode = ipaddress.IPv6Address(await reader.readexactly(16)).compressed, 'name-resolved-v6'
        else:
            host = (await reader.readexactly((await reader.readexactly(1))[0])).decode('idna')
            mode = 'name-at-proxy'
        port = struct.unpack('>H', await reader.readexactly(2))[0]
        writer.write(b'\x05\x00\x00\x01' + bytes(6))
        await writer.drain()
        up.mode = mode
        return host, port

    async def http_upstream(self, mode='relay', head_delay=0.0, tls=None, scheme='http'):
        """An HTTP upstream whose behaviour is chosen by ``mode``.

        ``relay``      - works
        ``gateway502`` - answers 502, as a real proxy does when the target is down
        ``silent``     - takes the request and closes without an answer
        ``garbage``    - answers something that is not HTTP at all
        ``auth``       - answers 407 to everything
        """
        up = Upstream(scheme)

        async def handler(reader, writer):
            self._track(up, writer)
            try:
                await self._serve_http(reader, writer, mode, head_delay, up)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError):
                pass  # a test may hang up at any moment
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a broken fake must not print a traceback
                pass
            finally:
                await shutdown(writer)

        up.port = await self.listen(handler, ssl=tls)
        self.ups.append(up)
        return up

    async def _serve_http(self, reader, writer, mode, head_delay, up):
        head = await reader.readuntil(b'\r\n\r\n')
        up.requests.append(head)
        if mode == 'silent':
            # Took the request and answered nothing: the case the old TCP-open
            # health check mistook for a working proxy.
            return
        if head_delay:
            await asyncio.sleep(head_delay)
        method, target = (head.split(b'\r\n')[0].split(b' ') + [b'', b''])[:2]
        if method == b'CONNECT':
            host, _, port = target.decode().rpartition(':')
            host = host.strip('[]')
            up.targets.append(('connect', host, int(port)))
            if mode != 'relay':
                return await self._answer(mode, writer)
            upstream = await asyncio.open_connection(host, int(port))
            writer.write(b'HTTP/1.1 200 OK\r\n\r\n')
            await writer.drain()
            return await splice(reader, writer, *upstream)
        url = httpx.URL(target.decode())
        up.targets.append(('forward', target.decode()))
        if mode != 'relay':
            return await self._answer(mode, writer)
        try:
            upstream = await asyncio.open_connection(url.host, url.port)
        except OSError:
            writer.write(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n')
            await writer.drain()
            return
        upstream[1].write(head.replace(target, url.raw_path, 1))
        await splice(reader, writer, *upstream)

        up.port = await self.listen(handler, ssl=tls)
        self.ups.append(up)
        return up

    async def _answer(self, mode, writer):
        answers = {
            'gateway502': b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n',
            'garbage': b'NOT-HTTP AT ALL\r\n\r\n',
            'auth': b'HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n',
        }
        writer.write(answers[mode])
        await writer.drain()
        await shutdown(writer)

    async def dead_upstream(self, scheme='socks5'):
        """A URL nothing is listening on."""
        server = await asyncio.start_server(lambda r, w: None, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()
        up = Upstream(scheme)
        up.port = port
        return up

    # --- clients -----------------------------------------------------------

    async def socks_client(self, address, *, user=b'', password=b'', target_host=None,
                           target_port=None, methods=b'\x00\x02'):
        """Speak SOCKS5 to the gateway.  Returns (granted, reader, writer)."""
        host, _, port = address.rpartition(':')
        target_host = target_host or '127.0.0.1'
        target_port = target_port or self.target
        reader, writer = await asyncio.open_connection(host.strip('[]'), int(port))
        writer.write(bytes([5, len(methods)]) + methods)
        await writer.drain()
        answer = await reader.readexactly(2)
        if answer[1] == 2:
            writer.write(bytes([1, len(user)]) + user + bytes([len(password)]) + password)
            await writer.drain()
            if await reader.readexactly(2) != b'\x01\x00':
                return False, reader, writer
        try:
            address_bytes = ipaddress.ip_address(target_host).packed
            atyp = b'\x04' if ':' in target_host else b'\x01'
        except ValueError:
            name = target_host.encode('idna')
            atyp, address_bytes = b'\x03', bytes([len(name)]) + name
        writer.write(b'\x05\x01\x00' + atyp + address_bytes + struct.pack('>H', int(target_port)))
        await writer.drain()
        reply = await reader.readexactly(4)
        if reply[1] != 0:
            return False, reader, writer
        skip = {1: 4, 4: 16}.get(reply[3])
        if skip is None:
            skip = (await reader.readexactly(1))[0]
        await reader.readexactly(skip + 2)
        return True, reader, writer

    async def http_client(self, address, request, auth=None):
        host, _, port = address.rpartition(':')
        reader, writer = await asyncio.open_connection(host.strip('[]'), int(port))
        if auth is not None:
            request = request.replace(
                b'\r\n\r\n', b'\r\nProxy-Authorization: Basic ' + base64.b64encode(auth) + b'\r\n\r\n', 1)
        writer.write(request)
        await writer.drain()
        return reader, writer

    async def get(self, address, path='/', scheme='http', auth=None, user='workbench'):
        """One proxied request through the gateway, as httpx would send it."""
        url = f'http://127.0.0.1:{self.target}{path}'
        target = f'{scheme}://{address}'
        if auth is not None:
            target = f'{scheme}://{user}:{auth}@{address}'
        async with httpx.AsyncClient(proxy=target, trust_env=False, timeout=10) as client:
            return await client.get(url)
