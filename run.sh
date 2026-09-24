#!/bin/sh
set -eu
cd "$(dirname "$0")"
export PYTHONUTF8=1
if [ ! -x .venv/bin/python ]; then
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Требуется Python 3.11+")'
    python3 -m venv .venv
fi
if command -v sha256sum >/dev/null 2>&1; then
    requirements_digest=$(sha256sum requirements.txt | cut -d' ' -f1)
else
    requirements_digest=$(shasum -a 256 requirements.txt | cut -d' ' -f1)
fi
current_digest=$(<.venv/.dependencies-ready 2>/dev/null || true)
if [ "$current_digest" != "$requirements_digest" ]; then
    .venv/bin/python -m pip install -r requirements.txt
    printf '%s\n' "$requirements_digest" > .venv/.dependencies-ready
fi
if [ "$#" -eq 0 ]; then
    set -- gui
fi
if [ "$1" = gui ]; then
    shift
    exec .venv/bin/python gui.py "$@"
fi
exec .venv/bin/python proxytool.py "$@"
