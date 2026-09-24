#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Требуется Python 3.11+")'
    python3 -m venv .venv
fi
if [ ! -f .venv/.dependencies-ready ]; then
    .venv/bin/python -m pip install -r requirements.txt
    touch .venv/.dependencies-ready
fi
if [ "$#" -eq 0 ]; then
    set -- gui
fi
if [ "$1" = gui ]; then
    shift
    exec .venv/bin/python gui.py "$@"
fi
exec .venv/bin/python proxytool.py "$@"
