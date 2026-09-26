"""The transport seam itself, through a real CONNECT proxy on loopback.

``probes`` never opens a socket: it builds a request, hands it to
``proxytool.Transport`` and interprets what comes back.  These four cases are
that contract, exercised over a real tunnel -- a websocket upgrade with a pong
and a connection held open are not things a stub can claim to have measured.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import pathlib
import sys
import threading
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from proxy_workbench import probes, proxytool
from tests.test_integration_end_to_end import LoopbackProxy

GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


class _Origin(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/ws':
            self._websocket()
            return
        if path == '/hold':
            self._hold()
            return
        body = b'x' * (2 << 20)
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        for start in range(0, len(body), 32768):
            try:
                self.wfile.write(body[start:start + 32768])
                self.wfile.flush()
            except OSError:
                return
            time.sleep(0.01)

    def _websocket(self):
        key = self.headers.get('Sec-WebSocket-Key', '')
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        self.send_response(101)
        self.send_header('Upgrade', 'websocket')
        self.send_header('Connection', 'Upgrade')
        self.send_header('Sec-WebSocket-Accept', accept)
        self.end_headers()
        buffer = b''
        try:
            while True:
                chunk = self.rfile.read1(4096)
                if not chunk:
                    return
                buffer += chunk
                while len(buffer) >= 2:
                    opcode = buffer[0] & 0x0F
                    length = buffer[1] & 0x7F
                    offset = 2
                    if length == 126:
                        length = int.from_bytes(buffer[2:4], 'big'); offset = 4
                    elif length == 127:
                        length = int.from_bytes(buffer[2:10], 'big'); offset = 10
                    masked = bool(buffer[1] & 0x80)
                    mask = buffer[offset:offset + 4] if masked else b''
                    if masked:
                        offset += 4
                    if len(buffer) < offset + length:
                        break
                    payload = buffer[offset:offset + length]
                    if masked:
                        payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
                    buffer = buffer[offset + length:]
                    if opcode == 0x9:
                        self.wfile.write(bytes((0x8A, len(payload))) + payload)
                        self.wfile.flush()
        except OSError:
            return

    def _hold(self):
        self.send_response(200)
        self.send_header('Content-Length', '5')
        self.end_headers()
        try:
            self.wfile.write(b'hello')
            self.wfile.flush()
            for _ in range(30):
                time.sleep(0.1)
                self.wfile.write(b'.')
                self.wfile.flush()
        except OSError:
            pass


class TransportSeamTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Origin)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.server.server_address[1]}'
        cls.proxy = LoopbackProxy().start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.proxy.stop()

    def options(self):
        return probes.validate_options({'whole_probe_timeout_s': 40, 'read_timeout_s': 12,
                                         'connect_timeout_s': 5, 'handshake_timeout_s': 6})

    def transport(self):
        return proxytool.Transport(self.proxy.url, {})

    async def test_send_is_a_bounded_request_through_the_tunnel(self):
        response = await self.transport().send(
            probes.ProbeRequest(url=f'{self.base}/', max_bytes=1024), options=self.options())
        self.assertEqual(response.status, 200)
        self.assertEqual(len(response.body), 1024, 'the byte budget is a real budget')
        self.assertIsNone(response.code)
        self.assertIsNotNone(response.ttfb_ms)

    async def test_download_fills_a_trace_and_produces_a_number(self):
        target = probes.validate_speed_target({'url': f'{self.base}/big', 'max_bytes': 2 << 20})
        measured = await probes.run_speed_test(target, self.options(), self.transport())
        self.assertEqual(measured.state, 'ok', measured)
        self.assertGreater(measured.mbps, 0)
        self.assertGreaterEqual(measured.chunks, 2, 'a single chunk is not a measurement')
        self.assertEqual(measured.connection, 'cold')

    async def test_websocket_upgrade_and_pong_are_measured(self):
        spec = probes.validate_capabilities([{
            'kind': 'websocket', 'id': 'ws-1', 'url': f'{self.base}/ws',
            'budget': {'max_pings': 2, 'ping_timeout_s': 5, 'handshake_timeout_s': 6,
                       'max_frame_bytes': 4096}}])[0]
        outcome = await probes.run_websocket(spec, self.options(), self.transport())
        self.assertEqual(outcome.state, 'ok', outcome.to_public())
        self.assertEqual(outcome.metrics.get('status'), 101)
        self.assertEqual(outcome.metrics.get('pongs'), 2)

    async def test_a_held_connection_is_held(self):
        spec = probes.validate_capabilities([{
            'kind': 'duration', 'id': 'h-1', 'url': f'{self.base}/hold',
            'budget': {'hold_s': 1.0, 'min_sustained_s': 0.5, 'min_bytes': 5}}])[0]
        outcome = await probes.run_duration(spec, self.options(), self.transport())
        self.assertEqual(outcome.state, 'ok', outcome.to_public())
        self.assertGreaterEqual(outcome.metrics.get('sustained_s'), 0.5)
        self.assertGreater(outcome.metrics.get('chunks'), 1)

    async def test_a_proxy_that_refuses_reports_a_status_not_an_exception(self):
        """A 502 from the proxy is an answer, and it is reported as one.

        The point is the shape of the failure path: a transport that raises
        would take the whole run down, and one that answers with nothing at all
        would let a broken proxy look like a quiet one.  The response carries a
        status, so ``probes`` records the exact status the proxy gave.
        """
        response = await self.transport().send(
            probes.ProbeRequest(url='http://127.0.0.1:1/', max_bytes=64),
            options=self.options())
        self.assertEqual(response.status, 502)
        self.assertEqual(response.body, b'')

    async def test_an_unreachable_proxy_is_a_code_and_not_a_crash(self):
        """A tunnel that cannot be opened at all is a code with a stage."""
        transport = proxytool.Transport('http://127.0.0.1:1', {})
        response = await transport.send(
            probes.ProbeRequest(url='http://example.invalid/', max_bytes=64),
            options=self.options())
        self.assertIsNone(response.status)
        self.assertIsNotNone(response.code, 'a refused tunnel is a code, not an exception')
        self.assertIsNotNone(response.stage)


if __name__ == '__main__':
    unittest.main()
