"""Where the application keeps its data and how it starts its own worker processes."""
from __future__ import annotations

import os
from pathlib import Path
import sys

PACKAGE = Path(__file__).resolve().parent
FROZEN = bool(getattr(sys, 'frozen', False))


def default_data(environ=None):
    """Use the same writable data folder for the CLI, GUI and desktop host."""
    # Lazy import: desktop uses PACKAGE, while command parsers call this helper.
    from .desktop import resolve_layout
    return resolve_layout(environ=environ, frozen_=FROZEN, package=PACKAGE).data


def worker_command(*args):
    """Command line that runs the CLI in a child process, frozen or not."""
    if FROZEN:
        return [sys.executable, *args]
    return [sys.executable, '-u', '-m', 'proxy_workbench', *args]
