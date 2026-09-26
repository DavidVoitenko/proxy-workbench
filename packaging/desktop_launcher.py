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

**A writable data folder.** ``proxy_workbench.paths.default_data`` answers
"next to the executable", which inside a read-only ``.app`` or under
``Program Files`` is a path nothing can be written to.  The product's own
resolver knows the per-user answer, so it is asked for that - and only when the
user has not already decided otherwise, so an explicit ``PROXY_WORKBENCH_DATA``,
``PROXY_WORKBENCH_PORTABLE`` or a portable marker still wins.
"""
import os
import sys


def point_data_at_a_writable_folder():
    """Give a frozen build the folders it is allowed to write to.

    All three are pinned, not just the data folder: a worker is started with a
    snapshot of this environment, and a child that resolved its own cache and
    log paths would put them somewhere the parent does not look.

    Returns the chosen data folder, or an empty string when nothing was
    changed - a source checkout, or a decision the user has already made.
    """
    from proxy_workbench import desktop
    if os.environ.get(desktop.DATA_ENV) or os.environ.get(desktop.PORTABLE_ENV):
        return ''
    if desktop.is_portable(desktop.portable_root()):
        return ''
    layout = desktop.resolve_layout()
    if layout.mode != 'per-user':
        # A source checkout keeps the data/ folder next to the project, and a
        # path the user has already set was honoured above.
        return ''
    os.environ[desktop.DATA_ENV] = str(layout.data)
    os.environ[desktop.CACHE_ENV] = str(layout.cache)
    os.environ[desktop.LOGS_ENV] = str(layout.logs)
    return str(layout.data)


def main():
    point_data_at_a_writable_folder()
    from proxy_workbench.__main__ import main as entry
    return entry()


if __name__ == '__main__':
    raise SystemExit(main())
