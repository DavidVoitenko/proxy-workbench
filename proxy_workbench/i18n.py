"""Language of terminal messages: Russian on Russian systems, English elsewhere.

Override with PROXY_WORKBENCH_LANG=en or PROXY_WORKBENCH_LANG=ru.
"""
from __future__ import annotations

import locale
import os


def detect(environ=None):
    environ = os.environ if environ is None else environ
    forced = environ.get('PROXY_WORKBENCH_LANG', '').strip().lower()
    if forced in ('en', 'ru'):
        return forced
    for name in ('LC_ALL', 'LC_MESSAGES', 'LANG'):
        if environ.get(name):
            return 'ru' if environ[name].lower().startswith('ru') else 'en'
    try:
        # Windows reports names such as "Russian_Russia" instead of LANG.
        system = locale.getlocale()[0] or ''
    except ValueError:
        system = ''
    return 'ru' if system.lower().startswith('ru') else 'en'


LANG = detect()


def tr(ru, en):
    """Pick the message for the terminal language."""
    return ru if LANG == 'ru' else en
