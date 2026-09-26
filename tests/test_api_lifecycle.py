"""Server-owned database handles are closed before data can be removed."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from proxy_workbench import api


class ApiLifecycleTests(unittest.TestCase):
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
