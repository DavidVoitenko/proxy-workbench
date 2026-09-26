"""Entry point of the single-binary build: no arguments open the application.

With arguments it is the CLI, because a build that ships one executable has to
keep the command line it has always had.

Without arguments it goes through ``proxy_workbench.desktop``, the same host
the windowed builds use, instead of straight to the interface.  That is what
makes the result an application and not a page: the menu bar, the single
instance, the opt-in login item and the sleep/wake hooks all live in that host,
and a build that skipped it would quietly have none of them.
"""
import sys

from proxy_workbench import desktop
from proxy_workbench.__main__ import main as cli_main

if __name__ == '__main__':
    raise SystemExit(cli_main() if sys.argv[1:] else desktop.main([]))
