"""Where the application keeps its data and how it starts its own worker processes."""
from __future__ import annotations

import os
from pathlib import Path
import sys

PACKAGE = Path(__file__).resolve().parent
FROZEN = bool(getattr(sys, 'frozen', False))


def default_data(environ=None):
    """The data folder: next to a source checkout or the .exe, otherwise a per-user folder."""
    environ = os.environ if environ is None else environ
    if environ.get('PROXY_WORKBENCH_DATA'):
        return Path(environ['PROXY_WORKBENCH_DATA']).expanduser()
    if FROZEN:
        return Path(sys.executable).resolve().parent / 'data'
    checkout = PACKAGE.parent
    if (checkout / 'pyproject.toml').is_file() and (checkout / 'Start.bat').is_file():
        return checkout / 'data'
    if os.name == 'nt':
        base = Path(environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')
    elif sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Application Support'
    else:
        base = Path(environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share')
    return base / 'proxy-workbench'


def worker_command(*args):
    """Command line that runs the CLI in a child process, frozen or not."""
    if FROZEN:
        return [sys.executable, *args]
    return [sys.executable, '-u', '-m', 'proxy_workbench', *args]
