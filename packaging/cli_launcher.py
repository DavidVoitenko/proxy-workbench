"""Entry point for the separate console CLI build.

A windowed executable has no console to print to, so the CLI ships as its own
binary instead of being the same one with an argument.  Everything below
``proxy_workbench.__main__`` is the CLI that the wheel already exposes, which
is what F23 asks to preserve.
"""
from proxy_workbench.__main__ import main

if __name__ == '__main__':
    raise SystemExit(main())
