"""Entry point for the windowed desktop build: a thin host around the browser GUI.

There is no second front end here on purpose.  The product's user interface is
the existing loopback GUI, and F23 allows a new desktop framework only for a
limitation this can name.  What this adds is delivery: writable per-user paths
instead of a folder next to the executable, a portable mode the user chooses,
and a worker that starts in a writable working directory.
"""
from proxy_workbench import desktop

if __name__ == '__main__':
    raise SystemExit(desktop.main())
