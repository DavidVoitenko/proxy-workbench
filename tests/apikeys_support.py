"""Shared fixture for the apikeys tests: a temporary database and a movable clock.

Not a test module on its own (``unittest discover`` only collects ``test*.py``).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from proxy_workbench import apikeys  # noqa: E402  (needs the path entry above)

# A canary that must never reach the database, the audit log, a file or a message.
CANARY_SECRET_MARK = 'pwk_CANARYCANARY'


class Clock:
    """A clock the test moves by hand, so expiry and grace windows need no sleep."""

    def __init__(self, start=1_700_000_000.0):
        self.value = float(start)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += float(seconds)
        return self.value

    def set(self, value):
        self.value = float(value)
        return self.value


def connect(tmp_path, name='workbench.db', *, shared=True):
    # check_same_thread=False mirrors the threaded local API server; apikeys
    # serialises every access with its own lock.
    conn = sqlite3.connect(str(Path(tmp_path) / name), check_same_thread=not shared)
    apikeys.ensure_schema(conn)
    return conn


def manager(conn, clock, **kwargs):
    return apikeys.ApiKeyManager(conn, now=clock, **kwargs)


def admin_principal(mgr, name='local admin'):
    """A real administrative key, issued the way the local GUI/CLI bootstrap does it."""
    issued = mgr.bootstrap_admin(local_trusted=True, name=name)
    return mgr.authenticate(issued.secret), issued
