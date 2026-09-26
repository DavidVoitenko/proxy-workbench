"""Entry point of every frozen GUI build: the desktop host, with a way out.

Two things happen here and neither belongs to the product.

**Routing.** A frozen build is one file, and the product inside it is three
entry points: the desktop host, the interface, and the command line.  The host
starts the application; the interface is ``gui`` or ``--no-desktop``; and
everything else is the CLI - which is not a nicety, it is how a check runs at
all.  ``proxy_workbench.gui`` starts its worker with
``[sys.executable, 'scan', ...]``, so a frozen executable that did not route
command words to the CLI would start an application that can never check a
single proxy.  The rule itself lives in ``proxy_workbench.__main__`` and is the
same one the installed console script uses.

The shared data-path resolver already handles installed and portable builds.
The host pins its child environment after parsing the user's ``--data``.
"""
from proxy_workbench.__main__ import main


if __name__ == '__main__':
    raise SystemExit(main())
