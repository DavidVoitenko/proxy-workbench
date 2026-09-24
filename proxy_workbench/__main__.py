"""`proxy-workbench` / `python -m proxy_workbench`: the GUI without arguments, the CLI otherwise."""
from __future__ import annotations

import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] == 'gui':
        from . import gui
        return gui.main(argv[1:])
    from . import proxytool
    return proxytool.main(argv)


if __name__ == '__main__':
    raise SystemExit(main())
