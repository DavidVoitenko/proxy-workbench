"""Server-owned database handles are closed before data can be removed."""
from pathlib import Path
import sqlite3
import socket
import tempfile
import threading
import unittest
from unittest import mock

from proxy_workbench import api


class ApiLifecycleTests(unittest.TestCase):
    def test_legacy_refusal_drains_a_fragmented_body_before_closing(self):
        with tempfile.TemporaryDirectory() as folder:
            server = api.make_api_server(Path(folder), port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with socket.create_connection(server.server_address, timeout=5) as client:
                    body = b'{"set":"quick"}'
                    client.sendall(b'POST /sources/set HTTP/1.1\r\nHost: 127.0.0.1\r\n'
                                   b'Content-Length: %d\r\n\r\n' % len(body) + body[:1])
                    client.settimeout(.1)
                    with self.assertRaises(socket.timeout):
                        client.recv(1)
                    client.sendall(body[1:])
                    client.settimeout(5)
                    response = b''
                    while chunk := client.recv(4096):
                        response += chunk
                    self.assertIn(b' 405 ', response.split(b'\r\n', 1)[0])
                    self.assertIn(b'read-only endpoint', response)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)

    def test_both_servers_close_their_key_database_exactly_once(self):
        for factory in (api.make_api_server, api.make_control_api):
            with self.subTest(factory=factory.__name__), tempfile.TemporaryDirectory() as folder:
                home = Path(folder)
                manager = api.key_manager(home)
                self.assertIsNotNone(manager)
                with mock.patch.object(api, 'key_manager', return_value=manager):
                    server = factory(home, port=0)
                self.assertEqual(manager.conn.execute('SELECT 1').fetchone()[0], 1)
                server.server_close()
                server.server_close()
                with self.assertRaises(sqlite3.ProgrammingError):
                    manager.conn.execute('SELECT 1')
                (home / 'proxies.sqlite3').unlink()

    def test_bind_failure_does_not_leave_the_key_database_open(self):
        for factory in (api.make_api_server, api.make_control_api):
            with self.subTest(factory=factory.__name__), tempfile.TemporaryDirectory() as folder:
                home = Path(folder)
                manager = api.key_manager(home)
                with mock.patch.object(api, 'key_manager', return_value=manager), \
                        mock.patch.object(api.ThreadingHTTPServer, 'server_bind', side_effect=OSError('busy')):
                    with self.assertRaises(OSError):
                        factory(home, port=0)
                with self.assertRaises(sqlite3.ProgrammingError):
                    manager.conn.execute('SELECT 1')
