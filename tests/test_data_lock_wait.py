"""A run started while a background task briefly holds the data folder must wait, not fail."""
from __future__ import annotations

import contextlib
import io
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from proxy_workbench import proxytool
from proxy_workbench.maintenance import exclusive_lock


def hold(path, seconds, ready):
    with exclusive_lock(path):
        ready.set()
        time.sleep(seconds)


class DataLockWaitTests(unittest.TestCase):
    def run_export(self, data):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = proxytool.main(['export', '--data', str(data)])
        return code, err.getvalue()

    def test_a_short_background_hold_is_waited_out(self):
        # The desktop app's job runner and scheduler take the lock every second;
        # a scan launched from the interface used to die with "folder busy".
        with tempfile.TemporaryDirectory() as tmp:
            ready = threading.Event()
            holder = threading.Thread(target=hold, args=(Path(tmp) / 'workbench.lock', 0.6, ready))
            holder.start()
            ready.wait(5)
            started = time.monotonic()
            _, err = self.run_export(tmp)
            holder.join()
            self.assertNotIn('already used by another run', err)
            self.assertNotIn('уже используется', err)
            self.assertGreaterEqual(time.monotonic() - started, 0.4)

    def test_a_folder_that_stays_busy_is_still_refused(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(proxytool, 'DATA_LOCK_WAIT_S', 0.3):
            ready = threading.Event()
            holder = threading.Thread(target=hold, args=(Path(tmp) / 'workbench.lock', 1.5, ready))
            holder.start()
            ready.wait(5)
            code, err = self.run_export(tmp)
            holder.join()
            self.assertEqual(code, 2)
            self.assertTrue('already used by another run' in err or 'уже используется' in err, err)


if __name__ == '__main__':
    unittest.main()
