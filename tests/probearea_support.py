"""Local sockets for the area-probes scenarios: a mock proxy and a real transport.

Everything here talks to 127.0.0.1 only.  No public proxy, DNSBL or third-party
service is contacted.  The transport is the seam ``probes.py`` documents and
``proxytool.py`` has to fill in production: this file is the proof that the
seam is enough to run a real end-to-end measurement.
"""
import asyncio
import base64
import contextlib
import json
import os
import socket
import struct
import threading
import time

import httpx

from proxy_workbench import probes as pr

_CHUNK = 16 * 1024


# --------------------------------------------------------------------------
# a tiny HTTP proxy: absolute-URI GET/HEAD, CONNECT, and configurable refusal
# --------------------------------------------------------------------------


class MockProxy:
    """A real listening HTTP proxy for local scenarios.

    ``mode`` decides what the endpoint can do, which is how "TCP-only",
    "speaks the protocol" and "carries a transfer" are told apart by a real
    socket instead of by a mock:

    ``open``       accepts a connection and closes it again (TCP only);
    ``silent``     accepts and says nothing (fails the handshake);
    ``refuse``     answers 403 to every forwarded request (proxy alive, target refused);
    ``http``       forwards plain HTTP;
    ``connect``    supports CONNECT and tunnels to an origin server.
    """

    def __init__(self, mode='connect', *, host='127.0.0.1', port=0, origin=None, force_connect=False):
        self.mode = mode
        self.host = host
        self.port = port
        self.origin = origin
        self.force_connect = force_connect
        self.connections = 0
        self.requests = 0
        self._server = None
        self._thread = None

    @property
    def url(self):
        return f'http://{self.host}:{self.port}'

    def start(self):
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((self.host, self.port))
        self._server.listen(64)
        self._server.settimeout(0.2)
        self.port = self._server.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, name='mock-proxy', daemon=True)
        self._thread.start()
        return self

    def _serve(self):
        while True:
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        conn.settimeout(10)
        try:
            if self.mode == 'open':
                conn.close()
                return
            if self.mode == 'silent':
                time.sleep(2.0)
                conn.close()
                return
            while True:
                head = self._read_head(conn)
                if not head:
                    return
                method, target, headers = head
                if method == 'CONNECT':
                    if self.mode != 'connect':
                        conn.sendall(b'HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n')
                        continue
                    self._tunnel(conn, target)
                    return
                self.requests += 1
                self._forward(conn, method, target, headers)
        except (OSError, ValueError):
            pass
        finally:
            with contextlib.suppress(OSError):
                conn.close()

    @staticmethod
    def _read_head(conn):
        data = b''
        while b'\r\n\r\n' not in data and len(data) < 65536:
            part = conn.recv(4096)
            if not part:
                return None
            data += part
        head, _, _ = data.partition(b'\r\n\r\n')
        lines = head.decode('latin-1').split('\r\n')
        if not lines or not lines[0]:
            return None
        pieces = lines[0].split(' ')
        if len(pieces) < 2:
            return None
        headers = {}
        for line in lines[1:]:
            if ':' in line:
                name, _, value = line.partition(':')
                headers[name.strip().lower()] = value.strip()
        return pieces[0].upper(), pieces[1], headers

    def _tunnel(self, conn, authority):
        host, _, port = authority.rpartition(':')
        try:
            upstream = socket.create_connection((host.strip('[]'), int(port)), timeout=5)
        except (OSError, ValueError):
            conn.sendall(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n')
            return
        conn.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
        self._pump(conn, upstream)

    def _pump(self, left, right):
        def copy(source, sink):
            try:
                while True:
                    chunk = source.recv(_CHUNK)
                    if not chunk:
                        break
                    sink.sendall(chunk)
            except OSError:
                pass
            finally:
                for sock in (source, sink):
                    with contextlib.suppress(OSError):
                        sock.shutdown(socket.SHUT_RDWR)

        threads = [threading.Thread(target=copy, args=pair, daemon=True)
                   for pair in ((left, right), (right, left))]
        for item in threads:
            item.start()
        for item in threads:
            item.join(timeout=15)

    def _forward(self, conn, method, target, headers):
        """Plain-HTTP forwarding: proves a transfer without CONNECT."""
        if self.mode == 'refuse':
            body = b'{"error":"proxy_refuses"}'
            conn.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Type: application/json\r\n'
                         + f'Content-Length: {len(body)}\r\n\r\n'.encode() + body)
            return
        from urllib.parse import urlsplit
        parsed = urlsplit(target)
        path = parsed.path or '/'
        if parsed.query:
            path += '?' + parsed.query
        try:
            upstream = socket.create_connection((parsed.hostname, parsed.port or 80), timeout=5)
        except OSError:
            conn.sendall(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n')
            return
        request = f'{method} {path} HTTP/1.1\r\nHost: {parsed.netloc}\r\nConnection: close\r\n\r\n'
        upstream.sendall(request.encode('latin-1'))
        self._pump(conn, upstream)

    def stop(self):
        if self._server is not None:
            with contextlib.suppress(OSError):
                self._server.close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.stop()


# --------------------------------------------------------------------------
# the transport seam
# --------------------------------------------------------------------------


class LoopbackTransport:
    """A real transport: sockets through a proxy, the seam ``probes`` documents.

    ``send`` performs one request; ``download`` streams a bounded body into a
    :class:`probes.TransferTrace`; ``websocket`` and ``hold`` fill the two
    capability traces.  Cold is the default: every call opens its own
    connection unless ``reuse=True`` was asked for.
    """

    def __init__(self, proxy=None, *, client=None):
        self.proxy = proxy
        self._client = client
        self._keepalive = None
        self.calls = 0

    def _proxy_url(self):
        return self.proxy.url if self.proxy else None

    def _new_client(self, timeout):
        if self.proxy is None:
            return httpx.AsyncClient(trust_env=False, timeout=timeout, follow_redirects=False)
        return httpx.AsyncClient(proxy=self._proxy_url(), trust_env=False, timeout=timeout,
                                 follow_redirects=False)

    async def send(self, request, *, options):
        self.calls += 1
        started = time.perf_counter()
        client = self._client or self._new_client(options.read_timeout_s)
        try:
            async with client.stream(request.method, request.url, headers=request.header_map(),
                                     content=request.body) as response:
                first = time.perf_counter()
                body = b''
                # Reading in a bounded chunk size is what makes a byte budget
                # a real budget: a transport that only checks after a 64 KiB
                # chunk would overshoot the ceiling by that much.
                chunk_size = max(1, min(8192, request.max_bytes)) if request.max_bytes else 8192
                async for chunk in response.aiter_bytes(chunk_size=chunk_size):
                    body += chunk
                    if len(body) >= request.max_bytes:
                        break
                finished = time.perf_counter()
                return pr.ProbeResponse(status=response.status_code,
                                        headers=tuple(response.headers.items()),
                                        body=body, url=str(response.url),
                                        connect_ms=round((first - started) * 1000, 2),
                                        ttfb_ms=round((first - started) * 1000, 2),
                                        transfer_ms=round((finished - first) * 1000, 2),
                                        total_ms=round((finished - started) * 1000, 2))
        except httpx.HTTPError as exc:
            stage, code = _stage_of(exc, request)
            return pr.ProbeResponse(code=code, stage=stage)
        finally:
            if self._client is None:
                await client.aclose()

    async def send_stage(self, request, *, options):
        """``run_stage`` uses the same seam; the proxy address is the target."""
        return await self.send(request, options=options)

    async def download(self, target, *, options, reuse=False):
        trace = pr.TransferTrace(url=target.url, connection='reused' if reuse else 'cold')
        started = time.perf_counter()
        trace.begin(started)
        client = self._client or self._new_client(options.read_timeout_s)
        try:
            async with client.stream('GET', target.url) as response:
                trace.status = response.status_code
                if response.status_code != 200:
                    return trace.fail(f'HTTP_{response.status_code}', time.perf_counter())
                async for chunk in response.aiter_bytes():
                    trace.add(len(chunk), time.perf_counter())
                    if trace.bytes >= target.max_bytes:
                        break
                trace.connect_ms = round((time.perf_counter() - started) * 1000, 2)
                return trace.finish(time.perf_counter())
        except httpx.HTTPError as exc:
            return trace.fail(_code_of(exc), time.perf_counter())
        finally:
            if self._client is None:
                await client.aclose()

    async def _open(self, url, timeout):
        """Open one connection, through the proxy when there is one.

        ``asyncio.open_connection`` has no proxy argument, so the CONNECT (or
        absolute-URI request for plain HTTP) is issued by hand — which is also
        what a production transport has to do for a raw socket.
        """
        host, port = _host_port(url)
        if self.proxy is None:
            return await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        phost, pport = _host_port(self.proxy.url)
        reader, writer = await asyncio.wait_for(asyncio.open_connection(phost, pport), timeout)
        # Always tunnel: the request line is written by the caller, so a
        # relative path stays valid on the far side of the tunnel.
        request = f'CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n'
        writer.write(request.encode('latin-1'))
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), timeout)
        fields = head.split(b' ')[1:2]
        status = int(fields[0]) if fields and fields[0].isdigit() else 0
        if status != 200:
            with contextlib.suppress(Exception):
                writer.close()
            raise OSError(f'proxy replied {status} for CONNECT {host}:{port}')
        return reader, writer

    async def websocket(self, request, *, options, spec):
        """A real RFC 6455 client: upgrade, then ping until the budget runs out."""
        trace = pr.WsTrace(connection=request.connection)
        started = time.perf_counter()
        writer = None
        try:
            reader, writer = await self._open(request.url, float(spec.budget['handshake_timeout_s']))
            key = base64.b64encode(os.urandom(16)).decode('ascii')
            host = request.url.split('//', 1)[1].split('/', 1)[0]
            path = '/' + request.url.split('//', 1)[1].split('/', 1)[1] if '/' in request.url.split('//', 1)[1] else '/'
            lines = [f'GET {path} HTTP/1.1', f'Host: {host}',
                     'Upgrade: websocket', 'Connection: Upgrade',
                     f'Sec-WebSocket-Key: {key}', 'Sec-WebSocket-Version: 13']
            for name, value in request.header_map().items():
                lines.append(f'{name}: {value}')
            writer.write(('\r\n'.join(lines) + '\r\n\r\n').encode('latin-1'))
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),
                                          float(spec.budget['handshake_timeout_s']))
            trace.status = int(head.split(b' ')[1])
            trace.handshake_ms = round((time.perf_counter() - started) * 1000, 2)
            response_headers = {}
            for line in head.decode('latin-1').split('\r\n')[1:]:
                if ':' in line:
                    name, _, value = line.partition(':')
                    response_headers[name.strip().lower()] = value.strip()
            trace.upgrade = response_headers.get('upgrade')
            trace.subprotocol = response_headers.get('sec-websocket-protocol')
            trace.extensions = response_headers.get('sec-websocket-extensions')
            if trace.status != 101:
                trace.closed_by_peer = True
                return trace
            wanted = int(spec.budget['max_pings'])
            deadline = time.perf_counter() + float(spec.budget['ping_timeout_s'])
            while trace.pongs < wanted and time.perf_counter() < deadline:
                await self._ws_ping(writer, reader, trace, deadline)
            if trace.pongs < wanted:
                trace.closed_by_peer = True
            return trace
        except (asyncio.TimeoutError, TimeoutError):
            trace.error = pr.READ_TIMEOUT
            trace.detail = 'пинг не получил ответа за отведённый срок'
            return trace
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            # The peer vanished mid-frame: a closed socket is an outcome, not
            # a transport crash.
            trace.closed_by_peer = True
            return trace
        except (OSError, httpx.HTTPError, ValueError) as exc:
            trace.error = _code_of(exc)
            return trace
        finally:
            if writer is not None:
                with contextlib.suppress(Exception):
                    writer.close()

    async def _ws_ping(self, writer, reader, trace, deadline):
        payload = os.urandom(4)
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        writer.write(bytes([0x89, 0x80 | len(payload)]) + mask + masked)
        await writer.drain()
        trace.pings_sent += 1
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return
            frame = await asyncio.wait_for(_read_ws_frame(reader), remaining)
            if frame is None:
                trace.closed_by_peer = True
                return
            opcode, data = frame
            trace.frames += 1
            trace.bytes_in += len(data)
            if opcode == 0xA:
                trace.pongs += 1
                if trace.rtt_ms is None:
                    trace.rtt_ms = 0.0
                return
            if opcode == 0x8:
                trace.closed_by_peer = True
                return

    async def hold(self, request, *, options, spec):
        """Open one connection, keep it for the budget, and time what happened."""
        trace = pr.HoldTrace(connection=request.connection)
        writer = None
        opened = time.perf_counter()
        try:
            reader, writer = await self._open(request.url, float(spec.budget.get('hold_s', 5)))
            trace.opened_at = opened
            host = request.url.split('//', 1)[1].split('/', 1)[0]
            path = '/' + request.url.split('//', 1)[1].split('/', 1)[1] if '/' in request.url.split('//', 1)[1] else '/'
            lines = [f'GET {path} HTTP/1.1', f'Host: {host}',
                     'Accept: application/octet-stream', 'Connection: close']
            for name, value in request.header_map().items():
                lines.append(f'{name}: {value}')
            writer.write(('\r\n'.join(lines) + '\r\n\r\n').encode('latin-1'))
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),
                                          float(spec.budget.get('hold_s', 5)))
            trace.status = int(head.split(b' ')[1])
            deadline = time.perf_counter() + float(spec.budget['hold_s'])
            while time.perf_counter() < deadline:
                chunk = await asyncio.wait_for(reader.read(_CHUNK),
                                               max(0.05, deadline - time.perf_counter()))
                if not chunk:
                    trace.closed_by_peer = True
                    break
                if trace.first_byte_at is None:
                    trace.first_byte_at = time.perf_counter()
                trace.bytes += len(chunk)
                trace.chunks += 1
                trace.last_byte_at = time.perf_counter()
            trace.closed_at = time.perf_counter()
            return trace
        except (asyncio.TimeoutError, TimeoutError):
            trace.closed_at = time.perf_counter()
            trace.detail = 'сокет не ответил'
            return trace
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            trace.closed_by_peer = True
            trace.closed_at = time.perf_counter()
            return trace
        except (OSError, httpx.HTTPError, ValueError) as exc:
            trace.error = _code_of(exc)
            return trace
        finally:
            if writer is not None:
                with contextlib.suppress(Exception):
                    writer.close()


def _host_port(url):
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    return parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80)


async def _read_ws_frame(reader):
    head = await reader.readexactly(2)
    opcode = head[0] & 0x0F
    size = head[1] & 0x7F
    masked = bool(head[1] & 0x80)
    if size == 126:
        size = int.from_bytes(await reader.readexactly(2), 'big')
    elif size == 127:
        size = int.from_bytes(await reader.readexactly(8), 'big')
    mask = await reader.readexactly(4) if masked else None
    data = await reader.readexactly(size) if size else b''
    if mask:
        data = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
    if opcode == 0x8:
        return None
    return opcode, data


def _code_of(exc):
    """Map a transport exception to a stable measurement code.

    The checks are exact, not substring: ``ConnectionResetError`` contains
    "Connect" but means the peer went away, not that the connect timed out.
    """
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return pr.READ_TIMEOUT
    name = type(exc).__name__
    if name in ('ConnectTimeout', 'ConnectError', 'ConnectionRefusedError'):
        return pr.CONNECT_TIMEOUT
    if name in ('ReadTimeout', 'ReadError', 'RemoteProtocolError'):
        return pr.READ_TIMEOUT
    if name in ('ProxyError', 'UnsupportedProtocol'):
        return pr.HANDSHAKE_PROTOCOL
    if 'SSL' in name or 'Certificate' in name:
        return pr.HANDSHAKE_PROTOCOL
    if isinstance(exc, OSError):
        return pr.UNREACHABLE
    return pr.UNREACHABLE


def _stage_of(exc, request):
    name = type(exc).__name__
    if request.stage in ('tcp', 'handshake'):
        return request.stage, _code_of(exc)
    return 'target', _code_of(exc)


# --------------------------------------------------------------------------
# helpers shared by the scenario modules
# --------------------------------------------------------------------------


def show(label, value):
    print(f'  {label:<46} {value}')


def rule(title):
    print(f'\n--- {title} ' + '-' * max(0, 78 - len(title)))


@contextlib.contextmanager
def reference_probe():
    running = pr.serve_reference_probe()
    info = running.__enter__()
    try:
        yield info
    finally:
        running.__exit__(None, None, None)


def run(coro):
    """Run one coroutine to completion; a few scenarios are plain blocking work."""
    return asyncio.run(coro)


def json_of(payload):
    return json.dumps(payload, ensure_ascii=False, default=str)


__all__ = ['MockProxy', 'LoopbackTransport', 'show', 'rule', 'reference_probe', 'run',
           'json_of', 'struct']
