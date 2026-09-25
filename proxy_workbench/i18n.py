"""Language of terminal messages: Russian on Russian systems, English elsewhere.

Override with PROXY_WORKBENCH_LANG=en or PROXY_WORKBENCH_LANG=ru.
"""
from __future__ import annotations

import locale
import os
import sys


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


def utf8_output():
    """Write UTF-8 even where the system code page is not (Windows pipes, the .exe, redirected logs)."""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(encoding='utf-8', errors='replace')
            except (OSError, ValueError):
                pass


def tr(ru, en):
    """Pick the message for the terminal language."""
    return ru if LANG == 'ru' else en


# Canonical machine codes of the shared contract (CONTRACTS §5.4, §5.6).
# The code never changes with the language; only the text next to it does.
CODES = {
    'OK': ('измерение пройдено', 'measurement passed'),
    'E_TIME_UNKNOWN': ('время проверки неизвестно; перепроверьте адрес', 'check time is unknown; re-check the address'),
    'E_TIME_TTL_EXPIRED': ('доказательство истекло; нужна новая проверка', 'the evidence expired; a new check is required'),
    'E_TIME_TTL_MISSING': ('не записан срок годности; нужна новая проверка', 'no expiry was recorded; a new check is required'),
    'E_TIME_FUTURE': ('время проверки из будущего; строка подозрительна', 'the check time is in the future; the row is suspect'),
    'E_TIME_CLOCK_ROLLBACK': ('часы переведены назад; нужна новая проверка', 'the clock moved backwards; a new check is required'),
    'E_STATE_NO_OBSERVATION': ('нет завершённого измерения', 'no completed measurement'),
    'E_STATE_MEASUREMENT_FAILED': ('последнее измерение неуспешно', 'the last measurement failed'),
    'E_STATE_MIN_SUCCESS': ('надёжность ниже порога', 'reliability is below the threshold'),
    'E_STATE_ANONYMITY': ('анонимность ниже требуемой', 'anonymity is below the required level'),
    'E_STATE_REPUTATION_LISTED': ('адрес в списках', 'the address is listed'),
    'E_STATE_REPUTATION_UNKNOWN': ('репутация неизвестна при строгой политике', 'reputation is unknown under a strict policy'),
    'E_STATE_LATENCY': ('задержка выше допустимой', 'latency is above the allowed maximum'),
    'E_SCOPE_DENYLIST': ('адрес в локальном запрете', 'the address is on the local denylist'),
    'E_SCOPE_COLLECTION': ('строка из другой коллекции', 'the row belongs to another collection'),
    'E_SCOPE_PROFILE_REVISION': ('строка из другой ревизии профиля', 'the row belongs to another profile revision'),
    'E_SCOPE_NETWORK': ('строка измерена в другой сети', 'the row was measured on another network'),
    'E_SCOPE_PROTOCOL': ('протокол не запрошен', 'the protocol is not requested'),
    'E_SCOPE_COUNTRY': ('страна не запрошена или неизвестна', 'the country is not requested or unknown'),
    'E_SCOPE_HOSTING': ('хостинг-провайдер исключён', 'the address belongs to an excluded hosting provider'),
    'E_SCOPE_CAPABILITY': ('нужная возможность не подтверждена измерением', 'the required capability was not demonstrated'),
    'E_CONFLICT_ACCESS_REVISION': ('строка измерена с другой ревизией доступа', 'the row was measured with another access revision'),
    'E_LIMIT_BUDGET': ('исчерпан общий срок; повторите позже', 'the whole-call budget is spent; try again later'),
    'E_CONFLICT_REVISION': ('область изменилась; обновите таблицу', 'the scope changed; reload the table'),
    'UNREACHABLE': ('адрес недоступен по TCP', 'the address is unreachable over TCP'),
    'DNS_TIMEOUT': ('DNS не ответил вовремя', 'DNS did not answer in time'),
    'DNS_ERROR': ('ошибка DNS', 'DNS error'),
    'HTTP_3XX': ('сервис ответил перенаправлением', 'the service answered with a redirect'),
    'HTTP_4XX': ('сервис ответил ошибкой 4xx', 'the service answered with a 4xx error'),
    'HTTP_5XX': ('сервис ответил ошибкой 5xx', 'the service answered with a 5xx error'),
    'CONTENT_MISMATCH': ('ответ не содержит ожидаемого текста', 'the response does not contain the expected text'),
    'CHECK_FAILED': ('проверка не пройдена', 'the check did not pass'),
}


def code_text(code, lang=None):
    """Human text for a machine code, in the interface language.

    An unknown code is returned unchanged: the code is the contract, the text
    is only a convenience, and a missing translation must never hide the code.
    """
    language = LANG if lang is None else lang
    entry = CODES.get(code)
    if not entry:
        return code
    return entry[0] if language == 'ru' else entry[1]


# ``state_detail`` of a published set (CONTRACTS §4.3).  It answers the one
# question a user actually has when a list is empty: "nothing matched" and
# "everything expired" are different reasons (defect 3), so they get different
# words.  The values come from ``core.STATE_DETAILS`` plus the two reader
# states the interface adds when the pointer cannot be read at all.
STATE_DETAILS = {
    'ok': ('набор собран и принят', 'the set was built and accepted'),
    'rejected': ('строки отклонены политикой', 'rows were rejected by the policy'),
    'all_expired': ('все строки истекли; нужна новая проверка', 'every row expired; a new check is needed'),
    'all_untrusted': ('нет строк с подтверждённой чистотой', 'no row has confirmed cleanliness'),
    'all_failed': ('все строки провалили измерение', 'every row failed its measurement'),
    'nothing_in_scope': ('в области нет ни одного адреса', 'the scope holds no address at all'),
    'empty_no_match': ('ничего не подошло под фильтры', 'nothing matched the filters'),
    'no_snapshot': ('публикации ещё нет', 'nothing has been published yet'),
    'pointer_unreadable': ('указатель публикации не читается', 'the publication pointer cannot be read'),
}


def state_detail_text(detail, lang=None):
    """Human text for a ``state_detail`` value, or the value itself.

    Same rule as :func`code_text`: an unknown value is returned unchanged, so a
    new reason from the engine is visible as a reason instead of disappearing.
    """
    if not detail:
        return ''
    language = LANG if lang is None else lang
    entry = STATE_DETAILS.get(str(detail))
    if not entry:
        return str(detail)
    return entry[0] if language == 'ru' else entry[1]


def code_lines(*codes, lang=None):
    """``code — text`` pairs for logs, JSON reports and the terminal.

    A code without a translation is left out: repeating the code twice in a
    row helps nobody.
    """
    return [f'{code} — {code_text(code, lang)}' for code in codes
            if code and code in CODES]

