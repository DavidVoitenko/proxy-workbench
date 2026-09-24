"""SOCKS4 support for httpx, which only speaks HTTP and SOCKS5 proxies natively.

The SOCKS4 handshake happens inside a custom httpcore network backend: every
"direct" connection first goes to the proxy, asks it to CONNECT to the target
and then hands the tunnel to httpcore, which runs TLS and HTTP over it as usual.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
import struct

import httpcore
import httpx

REPLY_GRANTED = 0x5A
REPLY_SIZE = 8


class Socks4Error(httpcore.ProxyError):
    pass


async def resolve_ipv4(host, port):
    """SOCKS4 carries only IPv4; hostnames are resolved locally."""
    try:
        return ipaddress.IPv4Address(host).packed
    except ValueError:
        pass
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
    if not infos:
        raise httpcore.ConnectError(f'{host} has no IPv4 address for SOCKS4')
    return ipaddress.IPv4Address(infos[0][4][0]).packed


def connect_request(address, port):
    # VN=4, CD=1 (CONNECT), DSTPORT, DSTIP, empty USERID terminated by NUL.
    return struct.pack('>BBH4s', 4, 1, port, address) + b'\x00'


def check_reply(reply):
    if len(reply) != REPLY_SIZE or reply[0] not in (0, 4):
        raise Socks4Error('SOCKS4_BAD_REPLY')
    if reply[1] != REPLY_GRANTED:
        raise Socks4Error(f'SOCKS4_REJECTED_{reply[1]:#x}')


class Socks4Backend(httpcore.AsyncNetworkBackend):
    def __init__(self, proxy_host, proxy_port, inner=None):
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port
        self.inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        address = await resolve_ipv4(host, port)
        stream = await self.inner.connect_tcp(self.proxy_host, self.proxy_port, timeout=timeout,
                                              local_address=local_address, socket_options=socket_options)
        try:
            await stream.write(connect_request(address, port), timeout=timeout)
            reply = b''
            while len(reply) < REPLY_SIZE:
                chunk = await stream.read(REPLY_SIZE - len(reply), timeout=timeout)
                if not chunk:
                    raise Socks4Error('SOCKS4_CLOSED')
                reply += chunk
            check_reply(reply)
        except BaseException:
            await stream.aclose()
            raise
        return stream

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):  # pragma: no cover
        raise httpcore.ConnectError('SOCKS4 does not support unix sockets')

    async def sleep(self, seconds):
        await self.inner.sleep(seconds)


def transport(proxy, verify=True):
    """An httpx transport that tunnels every request through a socks4:// proxy."""
    url = httpx.URL(proxy)
    if url.scheme != 'socks4' or not url.host or not url.port:
        raise ValueError('expected socks4://host:port')
    context = verify if isinstance(verify, ssl.SSLContext) else httpx.create_ssl_context(verify=verify)
    result = httpx.AsyncHTTPTransport(verify=context, trust_env=False)
    # httpx has no public hook for a network backend, so swap the connection pool.
    result._pool = httpcore.AsyncConnectionPool(ssl_context=context, network_backend=Socks4Backend(url.host, url.port))
    return result
