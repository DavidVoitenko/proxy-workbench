"""Gateway wire regressions using temporary exports and in-memory streams."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from proxy_workbench import gateway
from tests.gateway_support import shutdown, write_export


OK = b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK'
CONTINUE = b'HTTP/1.1 100 Continue\r\n\r\n'


class StreamWriter:
    """A duplex peer for real StreamReaders, including TCP-style half-close."""
    def __init__(self, reader):
        self.reader = reader
        self.peer = None
        self.closed = False
        self.eof = False

    def write(self, data):
        if self.closed or self.eof or self.peer.closed:
            raise ConnectionError('closed mock stream')
        self.peer.reader.feed_data(data)

    async def drain(self):
        await asyncio.sleep(0)

    def can_write_eof(self):
        return True

    def write_eof(self):
        self.eof = True
        self.peer.reader.feed_eof()

    def close(self):
        self.closed = True
        self.reader.feed_eof()
        self.peer.reader.feed_eof()

    async def wait_closed(self):
        await asyncio.sleep(0)

    def get_extra_info(self, name, default=None):
        return ('127.0.0.1', 0) if name == 'peername' else default


def stream_pair():
    left = StreamWriter(asyncio.StreamReader())
    right = StreamWriter(asyncio.StreamReader())
    left.peer, right.peer = right, left
    return (left.reader, left), (right.reader, right)


class WireCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.mock_tasks = set()
        self.mock_services = {}
        self.gateways = []
        self.client_tasks = []
        patcher = patch.object(gateway.asyncio, 'open_connection', self.open_mock)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        for running in self.gateways:
            await running.shutdown(0)
        await asyncio.gather(*self.client_tasks, return_exceptions=True)
        for task in tuple(self.mock_tasks):
            task.cancel()
        results = await asyncio.gather(*self.mock_tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                raise result

    async def mock_service(self, callback):
        port = 10000 + len(self.mock_services)
        self.mock_services[port] = callback
        return port

    async def open_mock(self, host, port, **options):
        self.assertEqual(host, '127.0.0.1')
        callback = self.mock_services[port]
        client, remote = stream_pair()

        async def serve(reader, writer):
            try:
                await callback(reader, writer)
            except (OSError, asyncio.IncompleteReadError):
                pass
            finally:
                await shutdown(writer)

        self.mock_tasks.add(asyncio.create_task(serve(*remote)))
        return client

    async def start_forwarder(self, callback, scheme='http', **options):
        port = await self.mock_service(callback)
        proxy = f'{scheme}://127.0.0.1:{port}'
        write_export(self.home, [proxy])
        options.setdefault('response_timeout', 0.5)
        options.setdefault('idle_timeout', 5)
        pool = gateway.Pool(self.home)
        pool.refresh()
        running = gateway.Gateway(pool, **options)
        self.gateways.append(running)
        return running, proxy

    async def client(self, running, payload):
        client, remote = stream_pair()
        self.client_tasks.append(asyncio.create_task(running.handle(*remote)))
        reader, writer = client
        writer.write(payload)
        await writer.drain()
        self.addAsyncCleanup(shutdown, writer)
        return reader, writer

    async def request(self, running, headers=b'Content-Length: 4', body=b''):
        return await self.client(
            running, b'POST http://127.0.0.1/upload HTTP/1.1\r\nHost: localhost\r\n'
            + headers + b'\r\n\r\n' + body)

    async def assert_released(self, running):
        await asyncio.wait_for(asyncio.gather(*self.client_tasks), 2)
        self.assertFalse(running.pool.active)
        self.assertFalse(running.streams)
        self.assertFalse(running.tasks)


class HttpStreamingTests(WireCase):
    async def test_post_body_reaches_upstream_before_the_response(self):
        received = []

        async def upstream(reader, writer):
            await reader.readuntil(b'\r\n\r\n')
            received.append(await reader.readexactly(4))
            writer.write(OK)
            await writer.drain()

        running, proxy = await self.start_forwarder(upstream)
        reader, writer = await self.request(running, body=b'BODY')
        writer.write_eof()
        answer = await asyncio.wait_for(reader.read(), 2)
        self.assertEqual(answer, OK, 'a POST must not get 504 followed by a late 200')
        self.assertEqual(received, [b'BODY'])
        self.assertEqual(running.pool.report(proxy)['ok'], 1)
        await self.assert_released(running)

    async def test_fragmented_body_streams_through_a_socks_tunnel(self):
        first_chunk = asyncio.Event()
        received = []

        async def upstream(reader, writer):
            await reader.readexactly(3)
            writer.write(b'\x05\x00')
            await writer.drain()
            await reader.readexactly(10)
            writer.write(b'\x05\x00\x00\x01' + bytes(6))
            await writer.drain()
            await reader.readuntil(b'\r\n\r\n')
            received.append(await reader.readexactly(2))
            first_chunk.set()
            received.append(await reader.readexactly(2))
            writer.write(OK)
            await writer.drain()

        running, _proxy = await self.start_forwarder(upstream, scheme='socks5')
        reader, writer = await self.request(running, body=b'BO')
        await asyncio.wait_for(first_chunk.wait(), 2)
        writer.write(b'DY')
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), OK)
        self.assertEqual(received, [b'BO', b'DY'])
        await self.assert_released(running)

    async def test_chunked_upload_preserves_chunks_and_trailers(self):
        body = b'2\r\nBO\r\n2\r\nDY\r\n0\r\nX-Checksum: done\r\n\r\n'
        received = []

        async def upstream(reader, writer):
            received.append(await reader.readuntil(b'\r\n\r\n'))
            received.append(await reader.readexactly(len(body)))
            writer.write(OK)
            await writer.drain()

        running, _proxy = await self.start_forwarder(upstream)
        reader, _writer = await self.request(
            running, headers=b'Transfer-Encoding: chunked\r\nTrailer: X-Checksum', body=body)
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), OK)
        self.assertIn(b'Transfer-Encoding: chunked', received[0])
        self.assertEqual(received[1], body)

    async def test_expect_continue_allows_the_client_to_send_its_body(self):
        received = []

        async def upstream(reader, writer):
            await reader.readuntil(b'\r\n\r\n')
            writer.write(CONTINUE)
            await writer.drain()
            received.append(await reader.readexactly(4))
            writer.write(OK)
            await writer.drain()

        running, proxy = await self.start_forwarder(upstream)
        reader, writer = await self.request(
            running, headers=b'Content-Length: 4\r\nExpect: 100-continue')
        self.assertEqual(await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 2), CONTINUE)
        self.assertEqual(running.pool.report(proxy)['ok'], 0,
                         'an interim response is not the final health verdict')
        writer.write(b'BODY')
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), OK)
        self.assertEqual(received, [b'BODY'])
        self.assertEqual(running.pool.report(proxy)['ok'], 1)

    async def test_informational_responses_do_not_hide_a_final_upstream_failure(self):
        hints = b'HTTP/1.1 103 Early Hints\r\nLink: </style.css>; rel=preload\r\n\r\n'
        failed = b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n'

        async def upstream(reader, writer):
            await reader.readuntil(b'\r\n\r\n')
            writer.write(CONTINUE + hints)
            await writer.drain()
            await reader.readexactly(4)
            writer.write(failed)
            await writer.drain()

        running, proxy = await self.start_forwarder(upstream)
        reader, _writer = await self.request(running, body=b'BODY')
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), CONTINUE + hints + failed)
        self.assertEqual(running.pool.report(proxy), {'ok': 0, 'failed': 0, 'target_failed': 1})
        await self.assert_released(running)

    async def test_early_final_response_does_not_wait_for_the_upload(self):
        refused = b'HTTP/1.1 413 Content Too Large\r\nContent-Length: 0\r\n\r\n'

        async def upstream(reader, writer):
            await reader.readuntil(b'\r\n\r\n')
            writer.write(refused)
            await writer.drain()

        running, _proxy = await self.start_forwarder(upstream)
        reader, _writer = await self.request(running, headers=b'Content-Length: 1000000')
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), refused)
        await self.assert_released(running)

    async def test_timeout_closes_without_late_response_or_replay(self):
        respond = asyncio.Event()
        received = []

        async def upstream(reader, writer):
            received.append(await reader.readuntil(b'\r\n\r\n'))
            await respond.wait()
            writer.write(OK)
            await writer.drain()

        running, proxy = await self.start_forwarder(upstream, response_timeout=0.05)
        reader, _writer = await self.request(running)
        head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 2)
        self.assertTrue(head.startswith(b'HTTP/1.1 504 '))
        respond.set()
        remainder = await asyncio.wait_for(reader.read(), 2)
        self.assertNotIn(b'HTTP/1.1', remainder, 'a generated error must end the exchange')
        self.assertEqual(len(received), 1)
        self.assertEqual(running.pool.stats['retries'], 0)
        self.assertEqual(running.pool.report(proxy)['target_failed'], 1)
        self.assertFalse(running.pool.active)

    async def test_shutdown_cancels_the_body_pump_and_releases_its_lease(self):
        body_seen = asyncio.Event()
        disconnected = asyncio.Event()

        async def upstream(reader, writer):
            await reader.readuntil(b'\r\n\r\n')
            await reader.readexactly(2)
            body_seen.set()
            await reader.read()
            disconnected.set()

        running, _proxy = await self.start_forwarder(upstream, response_timeout=30)
        reader, _writer = await self.request(running, body=b'BO')
        await asyncio.wait_for(body_seen.wait(), 2)
        await asyncio.wait_for(running.shutdown(0), 2)
        await asyncio.wait_for(disconnected.wait(), 2)
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b'')
        self.assertFalse(running.pool.active)
        self.assertFalse(running.streams)
        self.assertFalse(running.tasks)


class TunnelLifecycleTests(WireCase):
    async def test_tunnel_half_close_keeps_the_other_direction_open(self):
        received = []

        async def upstream(reader, writer):
            await reader.readuntil(b'\r\n\r\n')
            writer.write(b'HTTP/1.1 200 Connection established\r\n\r\nEARLY')
            await writer.drain()
            writer.write_eof()
            received.append(await reader.read())

        running, proxy = await self.start_forwarder(upstream)
        reader, writer = await self.client(running, b'CONNECT 127.0.0.1:80 HTTP/1.1\r\n\r\n')
        self.assertIn(b' 200 ', await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 2))
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b'EARLY')
        writer.write(b'AFTER_EOF')
        await writer.drain()
        writer.write_eof()
        await self.assert_released(running)
        self.assertEqual(received, [b'AFTER_EOF'])
        self.assertEqual(running.pool.report(proxy)['ok'], 1)

    async def test_session_cap_cancels_both_tunnel_pumps(self):
        disconnected = asyncio.Event()

        async def upstream(reader, writer):
            await reader.readuntil(b'\r\n\r\n')
            writer.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
            await writer.drain()
            await reader.read()
            disconnected.set()

        running, _proxy = await self.start_forwarder(upstream, max_session=0.05)
        reader, _writer = await self.client(running, b'CONNECT 127.0.0.1:80 HTTP/1.1\r\n\r\n')
        self.assertIn(b' 200 ', await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 2))
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b'')
        await asyncio.wait_for(disconnected.wait(), 2)
        await self.assert_released(running)
        self.assertEqual(running.pool.stats['capped'], 1)


class Socks4aTests(WireCase):
    async def test_hostname_follows_the_empty_userid_and_does_not_resolve_locally(self):
        seen = []

        async def strict_proxy(reader, writer):
            seen.append(await reader.readexactly(8))
            seen.append(await reader.readuntil(b'\x00'))
            seen.append(await reader.readuntil(b'\x00'))
            writer.write(b'\x00\x5a' + bytes(6) + b'TARGET')
            await writer.drain()

        port = await self.mock_service(strict_proxy)
        with patch.object(gateway, 'resolve', side_effect=AssertionError('unexpected DNS')):
            reader, writer = await asyncio.wait_for(
                gateway.open_tunnel(f'socks4a://127.0.0.1:{port}', 'target.invalid', 80), 0.5)
        self.addAsyncCleanup(shutdown, writer)
        self.assertEqual(seen, [b'\x04\x01\x00\x50\x00\x00\x00\x01',
                                b'\x00', b'target.invalid\x00'])
        self.assertEqual(await reader.read(), b'TARGET')


class RecordingWriter:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        await asyncio.sleep(0)

    def close(self):
        self.closed = True


class SocksReplyTests(unittest.IsolatedAsyncioTestCase):
    async def open(self, reply, credentials=None):
        reader = asyncio.StreamReader()
        reader.feed_data(reply)
        reader.feed_eof()
        writer = RecordingWriter()
        self.writer = writer
        with patch.object(gateway.asyncio, 'open_connection', return_value=(reader, writer)):
            return await gateway.open_tunnel('socks5h://127.0.0.1:1080', 'target.invalid', 80,
                                             credentials=credentials)

    async def test_greeting_must_select_an_offered_method_with_version_five(self):
        auth = SimpleNamespace(socks5_greeting=b'\x05\x01\x02',
                               socks5_auth=b'\x01\x01u\x01p')
        no_auth = SimpleNamespace(socks5_greeting=b'\x05\x01\x00',
                                  socks5_auth=auth.socks5_auth)
        for greeting, credentials in ((b'\x04\x00', None), (b'\x05\xff', None),
                                      (b'\x05\x01', None), (b'\x05\x00', auth),
                                      (b'\x05\x02', no_auth), (b'\x05', None)):
            with self.subTest(greeting=greeting, authenticated=credentials is auth):
                tail = (b'\x01\x00' if greeting == b'\x05\x02' else b'')
                if len(greeting) == 2:
                    tail += b'\x05\x00\x00\x01' + bytes(6)
                with self.assertRaises(gateway.UpstreamError):
                    await self.open(greeting + tail, credentials)
                self.assertTrue(self.writer.closed)
                self.assertEqual(bytes(self.writer.data),
                                 credentials.socks5_greeting if credentials else b'\x05\x01\x00')

    async def test_auth_reply_requires_version_one_and_success(self):
        auth = SimpleNamespace(socks5_greeting=b'\x05\x01\x02',
                               socks5_auth=b'\x01\x01u\x01p')
        for reply in (b'\x05\x00', b'\x01\x01', b'\x01'):
            with self.subTest(reply=reply):
                with self.assertRaises(gateway.UpstreamError):
                    await self.open(b'\x05\x02' + reply, auth)
                self.assertTrue(self.writer.closed)
                self.assertEqual(bytes(self.writer.data), auth.socks5_greeting + auth.socks5_auth)

    async def test_connect_reply_rejects_invalid_version_reserved_type_and_length(self):
        replies = [b'\x04\x00\x00\x01' + bytes(6),
                   b'\x05\x00\x01\x01' + bytes(6),
                   b'\x05\x00\x00\x03\x00' + bytes(2),
                   b'\x05\x00\x00\x01' + bytes(5),
                   b'\x05\x00\x00\x04' + bytes(17),
                   b'\x05\x00\x00\x03\x05short\x00',
                   b'\x05\x05\x00\x01' + bytes(6)]
        replies.extend(b'\x05\x00\x00' + bytes([kind]) + b'\x01x\x00\x50'
                       for kind in (0, 2, 255))
        for reply in replies:
            with self.subTest(reply=reply):
                with self.assertRaises(gateway.UpstreamError):
                    await self.open(b'\x05\x00' + reply)
                self.assertTrue(self.writer.closed)

    async def test_valid_reply_types_leave_target_bytes_untouched(self):
        addresses = (b'\x01\x7f\x00\x00\x01', b'\x04' + bytes(15) + b'\x01',
                     b'\x03\x09localhost')
        for address in addresses:
            with self.subTest(address=address):
                reader, writer = await self.open(
                    b'\x05\x00\x05\x00\x00' + address + b'\x12\x34TARGET')
                self.assertEqual(await reader.read(), b'TARGET')
                self.assertFalse(writer.closed)
                writer.close()

    async def test_successful_authentication_continues_to_the_tunnel(self):
        auth = SimpleNamespace(socks5_greeting=b'\x05\x01\x02',
                               socks5_auth=b'\x01\x01u\x01p')
        reader, writer = await self.open(
            b'\x05\x02\x01\x00\x05\x00\x00\x01' + bytes(6) + b'TARGET', auth)
        self.assertEqual(await reader.read(), b'TARGET')
        self.assertEqual(bytes(writer.data), auth.socks5_greeting + auth.socks5_auth
                         + b'\x05\x01\x00\x03\x0etarget.invalid\x00\x50')
        self.assertFalse(writer.closed)
        writer.close()


if __name__ == '__main__':
    unittest.main()
