"""Entry point of the single-binary build: one executable, all three layers.

It is the routing of ``packaging/desktop_launcher.py`` and nothing else, kept
as a separate file because ``packaging/proxy-workbench.spec`` - the
long-standing single-file build - points at this name.
"""
import sys
from pathlib import Path

# The frozen analysis runs from a copy of this file with no project directory
# on the path, so the sibling module is found by this file's own location
# rather than by whatever PyInstaller happened to put on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from desktop_launcher import main

if __name__ == '__main__':
    raise SystemExit(main())
