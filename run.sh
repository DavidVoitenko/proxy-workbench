#!/bin/sh
set -eu
cd "$(dirname "$0")"
export PYTHONUTF8=1

fail() {
    printf '%s\n' "$1" >&2
    exit 1
}

[ -f requirements.txt ] || fail 'requirements.txt не найден; запускайте launcher из корня проекта.'
if [ -x .venv/bin/python ]; then
    .venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
        || fail 'Существующий .venv создан неподдерживаемым Python. Удалите только .venv и запустите launcher снова.'
else
    command -v python3 >/dev/null 2>&1 || fail 'Python 3.11+ не найден: установите Python и повторите запуск.'
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
        || fail 'Требуется Python 3.11+.'
    python3 -m venv .venv || fail 'Не удалось создать .venv. Проверьте права на папку и установку Python.'
fi

requirements_digest=$(.venv/bin/python -c 'import hashlib; print(hashlib.sha256(open("requirements.txt", "rb").read()).hexdigest())')
current_digest=''
if [ -f .venv/.dependencies-ready ]; then
    current_digest=$(cat .venv/.dependencies-ready)
fi
if [ "$current_digest" != "$requirements_digest" ]; then
    .venv/bin/python -m pip install --requirement requirements.txt \
        || fail 'Не удалось установить зависимости. Проверьте сеть/PyPI и повторите запуск.'
    printf '%s\n' "$requirements_digest" > .venv/.dependencies-ready
fi
exec .venv/bin/python -m proxy_workbench "$@"
