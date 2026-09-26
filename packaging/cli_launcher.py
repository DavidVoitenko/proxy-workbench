"""Entry point for the separate console CLI build.

A windowed executable has no console to print to, so the CLI ships as its own
binary instead of being the same one with an argument.  Everything below
``proxy_workbench.__main__`` is the CLI that the wheel already exposes, which
is what F23 asks to preserve.

It also shares one rule with the GUI build: an installed binary defaults to the
user's own folders instead of a folder next to the executable, which is
read-only under ``Program Files`` and would turn every command without an
explicit ``--data`` into a permission error.  An explicit ``PROXY_WORKBENCH_DATA``,
``PROXY_WORKBENCH_PORTABLE`` or a portable marker still wins.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from desktop_launcher import point_data_at_a_writable_folder
from proxy_workbench.__main__ import main as entry

if __name__ == '__main__':
    point_data_at_a_writable_folder()
    raise SystemExit(entry())
