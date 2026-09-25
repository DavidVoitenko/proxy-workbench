"""Failure classification, the zero-result funnel and local diagnostics.

The module answers one question without a traceback: *why did this run produce
what it produced, and what does the user do next?*  It is a leaf module — it
imports no engine, opens no socket on import, and writes nothing unless the
caller explicitly saves a bundle.

Contract notes (docs/integration/CONTRACTS.ru.md):

* §5.4 — codes are machine-readable and the text is translated separately
  through ``i18n.tr``, so a translation change never renames a code.  Set-level
  codes keep the ``E_<DOMAIN>_<REASON>`` form of the canon; codes stored inside a
  result row stay bare, exactly as the canon lists them (``UNREACHABLE``,
  ``CONTENT_MISMATCH``, ...).
* §2.4 — the four time states (``time_ok``/``time_unknown``/``time_future``/
  ``clock_rollback``) plus an expired TTL are separate outcomes, not a fake
  "fresh" row.
* F10 — the stages are ordered so a run shows *where* it lost its proxies, and
  every cause carries a concrete recovery action.
* F25 — the bundle is local, redacted, previewed before it is written, and is
  never sent anywhere by this module.
"""
from __future__ import annotations

import dataclasses
import json
import math
import os
import platform
import re
import socket
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from .i18n import tr

__all__ = [
    'STAGES', 'FUNNEL_STAGES', 'CODES', 'CODE_STAGES', 'EXCEPTION_CODES', 'TIME_STATES',
    'TIME_OK', 'TIME_UNKNOWN', 'TIME_FUTURE', 'CLOCK_ROLLBACK', 'TIME_EXPIRED', 'TIME_TTL_MISSING',
    'OBSERVATION_MISSING', 'Code', 'ErrorHelp', 'ActionableError', 'explain_error', 'actionable',
    'help_for', 'help_entries', 'codes_for_stage', 'code_stage', 'code_title',
    'code_action', 'is_known_code', 'classify_error', 'classification', 'stage_of',
    'StageCounters', 'SourceReport', 'Funnel', 'build_funnel', 'record_source_report',
    'data_state', 'explain_zero', 'funnel_explanation', 'ZeroResult',
    'ControlLimits', 'ControlProbe', 'ControlVerdict', 'DEFAULT_CONTROL_LIMITS',
    'check_control', 'attribution_for', 'Redaction', 'DEFAULT_REDACTION',
    'RedactionNote', 'redact_value', 'DiagnosticBundle', 'BUNDLE_SCHEMA_VERSION',
    'build_bundle', 'HealthCheck', 'HealthReport', 'health_report',
    'FixtureRecipe', 'fixture_recipe',
]

BUNDLE_SCHEMA_VERSION = 1

#: A legacy row without a recorded lifetime is read with this synthetic max-age,
#: mirroring ``proxytool.MIN_FRESHNESS_SECONDS``.  It is a parameter, not an
#: optimum: CONTRACTS §2.4 only promises a visible, configurable max-age.
LEGACY_MAX_AGE_SECONDS = 2 * 60 * 60

#: How far a ``checked_at`` may sit in the future before the row is suspicious
#: rather than very fresh (CONTRACTS §2.4, ``time_future``).
CLOCK_SKEW_SECONDS = 60.0

#: Time states of a row.  These are the same strings as ``core.TIME_STATES`` on
#: purpose: two modules must not invent two names for the same axis.
TIME_OK = 'time_ok'
TIME_UNKNOWN = 'time_unknown'
TIME_FUTURE = 'time_future'
CLOCK_ROLLBACK = 'clock_rollback'
TIME_EXPIRED = 'time_expired'
TIME_TTL_MISSING = 'time_ttl_missing'
TIME_STATES = frozenset({TIME_OK, TIME_UNKNOWN, TIME_FUTURE, CLOCK_ROLLBACK, TIME_EXPIRED, TIME_TTL_MISSING})

#: A row without samples and without an error carries no measurement at all.
OBSERVATION_MISSING = 'observation_missing'

#: Named stages.  ``FUNNEL_STAGES`` is the ordered measurement funnel a proxy
#: walks through; the remaining two names belong to the set around it.
STAGES = ('source', 'download', 'parser', 'scope', 'device_network', 'dns', 'tcp',
          'handshake', 'tls', 'target', 'assertion', 'auth', 'rate_limit', 'budget',
          'freshness', 'environment', 'export')

FUNNEL_STAGES = ('source', 'download', 'parser', 'scope', 'device_network', 'dns',
                 'tcp', 'handshake', 'tls', 'target', 'assertion', 'auth',
                 'rate_limit', 'budget', 'freshness')

#: Stages whose loss says something about *this machine or the target*, not about
#: the proxy address.  A loss here must not turn into a reputation mark.
ENVIRONMENT_STAGES = frozenset({'device_network', 'rate_limit'})


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _pick(ru: str, en: str, lang: str | None = None) -> str:
    """Translated text for an explicit language, or for the terminal language."""
    if lang is None:
        return tr(ru, en)
    return ru if lang == 'ru' else en


@dataclasses.dataclass(frozen=True)
class Code:
    """One documented error code: what it means and what the user can do."""

    code: str
    stage: str
    domain: str
    params: tuple[str, ...]
    title_ru: str
    title_en: str
    action_ru: str
    action_en: str

    def title(self, lang: str | None = None) -> str:
        return _pick(self.title_ru, self.title_en, lang)

    def action(self, lang: str | None = None) -> str:
        return _pick(self.action_ru, self.action_en, lang)


def _code(code: str, stage: str, domain: str, *, params: Sequence[str] = (),
          title_ru: str, title_en: str, action_ru: str, action_en: str) -> Code:
    return Code(code, stage, domain, tuple(params), title_ru, title_en, action_ru, action_en)


CODES: dict[str, Code] = {}


def _register(*entries: Code) -> None:
    for entry in entries:
        if entry.code in CODES:
            raise RuntimeError(f'duplicated error code: {entry.code}')
        if entry.stage not in STAGES:
            raise RuntimeError(f'unknown stage for {entry.code}: {entry.stage}')
        CODES[entry.code] = entry


# --- bare codes stored in a result row, plus environment and set-level codes ----
_register(
    _code('DNS_ERROR', 'dns', 'UPSTREAM', params=('host',), title_ru='Имя не разрешилось в адрес',
          title_en='Name did not resolve to an address',
          action_ru='Проверьте DNS и формат имени: без A/AAAA-записи адрес не измеряется.',
          action_en='Check DNS and the name format: without an A/AAAA record the address is not measured.'),
    _code('DNS_TIMEOUT', 'dns', 'UPSTREAM', params=('timeout_s',), title_ru='DNS не ответил вовремя',
          title_en='DNS did not answer in time',
          action_ru='Повторите позже или смените резолвер: слишком много параллельных имён перегружает DNS.',
          action_en='Retry later or change the resolver: too many parallel names overload DNS.'),
    _code('UNREACHABLE', 'tcp', 'UPSTREAM', title_ru='Адрес не принимает соединение',
          title_en='Address accepts no connection',
          action_ru='Проверьте, открыт ли порт, и не блокирует ли адрес ваш провайдер или сеть.',
          action_en='Check that the port is open and that your provider or network does not block the address.'),
    _code('TCP_TIMEOUT', 'tcp', 'UPSTREAM', params=('timeout_s',), title_ru='Нет ответа на подключение',
          title_en='No answer while connecting',
          action_ru='Увеличьте connect timeout или уменьшите число адресов в одной волне; порт может фильтроваться.',
          action_en='Raise the connect timeout or reduce the address batch; the port may be filtered.'),
    _code('TCP_REFUSED', 'tcp', 'UPSTREAM', title_ru='Соединение отклонено',
          title_en='Connection refused',
          action_ru='На этом порту никто не слушает. Проверьте порт источника или оставьте адрес для следующего круга.',
          action_en='Nothing listens on that port. Check the source port or leave the address for a later round.'),
    _code('TCP_RESET', 'tcp', 'UPSTREAM', title_ru='Соединение сброшено',
          title_en='Connection reset',
          action_ru='Адрес сбрасывает соединение сразу после accept. Повторите позже или исключите адрес.',
          action_en='The address resets right after accept. Retry later or exclude it.'),
    _code('TIMEOUT', 'target', 'UPSTREAM', params=('timeout_s', 'phase',), title_ru='Цель не ответила вовремя',
          title_en='Target did not answer in time',
          action_ru='Поднимите timeout или попробуйте другую цель: текущая может быть медленной или недоступной.',
          action_en='Raise the timeout or try another target: the current one may be slow or unreachable.'),
    _code('HANDSHAKE_ERROR', 'handshake', 'UPSTREAM', params=('scheme',), title_ru='Рукопожатие с прокси не удалось',
          title_en='Proxy handshake failed',
          action_ru='Схема не совпадает с тем, что говорит адрес. Проверьте http/socks и попробуйте другую схему.',
          action_en='The scheme does not match the address. Check http/socks and try the other scheme.'),
    _code('TLS_ERROR', 'tls', 'UPSTREAM', title_ru='Ошибка TLS',
          title_en='TLS error',
          action_ru='Проверьте системное время и цепочку сертификатов; для доверенного scope доступна своя CA.',
          action_en='Check the system clock and the certificate chain; a custom CA is available for a trusted scope.'),
    _code('TLS_CERT_INVALID', 'tls', 'UPSTREAM', params=('reason',), title_ru='Сертификат TLS не прошёл проверку',
          title_en='TLS certificate did not validate',
          action_ru='Отключайте проверку TLS только для доверенного собственного адреса; для публичных адресов это ошибка адреса.',
          action_en='Disable TLS verification only for a trusted own address; for public addresses this is an address error.'),
    _code('HTTP_3XX', 'target', 'UPSTREAM', params=('status',), title_ru='Цель ответила перенаправлением',
          title_en='Target answered with a redirect',
          action_ru='Проверьте адрес цели: он уводит в другое место или на страницу с капчей.',
          action_en='Check the target URL: it redirects elsewhere or to a captcha page.'),
    _code('HTTP_4XX', 'target', 'UPSTREAM', params=('status',), title_ru='Цель отклонила запрос',
          title_en='Target rejected the request',
          action_ru='Проверьте требуемый статус и заголовки цели: возможно, нужен другой код ответа.',
          action_en='Check the expected status and target headers: another response code may be required.'),
    _code('HTTP_5XX', 'target', 'UPSTREAM', params=('status',), title_ru='Цель вернула ошибку сервера',
          title_en='Target returned a server error',
          action_ru='Это ошибка цели, а не адреса. Попробуйте позже или смените цель.',
          action_en='That is a target failure, not an address failure. Retry later or change the target.'),
    _code('BODY_TOO_LARGE', 'target', 'UPSTREAM', params=('max_bytes',), title_ru='Ответ больше лимита',
          title_en='Response larger than the limit',
          action_ru='Поднимите max_bytes для этой цели или уменьшите объём загружаемого.',
          action_en='Raise max_bytes for this target or reduce how much is downloaded.'),
    _code('CONTENT_TRUNCATED', 'target', 'UPSTREAM', title_ru='Ответ оборвался на середине',
          title_en='Response was cut short',
          action_ru='Соединение рвётся при передаче. Это бывает у нестабильных адресов.',
          action_en='The connection breaks during transfer, which unstable addresses do.'),
    _code('CONTENT_MISMATCH', 'assertion', 'UPSTREAM', params=('needle',), title_ru='Ответ не содержит ожидаемое',
          title_en='Response does not contain the expected text',
          action_ru='Цель отдала другой контент. Обновите правило проверки или выберите другую цель.',
          action_en='The target served different content. Update the assertion or choose another target.'),
    _code('HASH_MISMATCH', 'assertion', 'UPSTREAM', params=('sha256',), title_ru='Хеш ответа не совпал',
          title_en='Response hash did not match',
          action_ru='Контент изменился или подменён. Обновите эталонный хеш, если это ожидаемо.',
          action_en='Content changed or was substituted. Refresh the expected hash if that is intended.'),
    _code('SCREEN_ERROR', 'parser', 'UPSTREAM', title_ru='Экран репутации не дал вердикта',
          title_en='Reputation screen gave no verdict',
          action_ru='DNSBL недоступен или ответил ошибкой. Вердикт остаётся unknown, адрес не помечается как плохой.',
          action_en='DNSBL was unavailable or errored. The verdict stays unknown; the address is not marked bad.'),
    _code('NO_DNSBL_ZONES', 'parser', 'UPSTREAM', title_ru='Нет настроенных зон DNSBL',
          title_en='No DNSBL zones configured',
          action_ru='Настройте зоны DNSBL в профиле или отключите строгий режим репутации.',
          action_en='Configure DNSBL zones in the profile or turn off the strict reputation mode.'),
    _code('AUTH_FAILED', 'auth', 'UPSTREAM', params=('scheme',), title_ru='Прокси отклонил учётные данные',
          title_en='Proxy rejected the credentials',
          action_ru='Проверьте логин и пароль доступа и перевыпустите пароль: старый результат проверки не переносится.',
          action_en='Check the access login and password and rotate it: an old check result is not carried over.'),
    _code('VAULT_LOCKED', 'auth', 'SECRET', title_ru='Хранилище секретов закрыто',
          title_en='Secret store is locked',
          action_ru='Откройте хранилище в приложении и повторите проверку: без доступа пароль не подставляется.',
          action_en='Unlock the store in the app and retry: without access no password is substituted.'),
    _code('RATE_LIMITED', 'rate_limit', 'UPSTREAM', params=('retry_after_s',), title_ru='Запрос отклонён по лимиту',
          title_en='Request refused by a rate limit',
          action_ru='Подождите указанное время и снизьте частоту; источник и цель считаются отдельно.',
          action_en='Wait the stated time and lower the rate; source and target are budgeted separately.'),
    _code('BUDGET_EXHAUSTED', 'budget', 'LIMIT', params=('kind', 'used', 'limit'), title_ru='Бюджет исчерпан',
          title_en='Budget exhausted',
          action_ru='Увеличьте бюджет запросов, байтов или времени либо уменьшите объём задания.',
          action_en='Raise the request, byte or time budget, or reduce the size of the job.'),
    _code('SCOPE_FILTERED', 'scope', 'STATE', params=('filter',), title_ru='Адрес не попал в область проверки',
          title_en='Address is outside the check scope',
          action_ru='Ослабьте фильтры (страна, протокол, hosting) или добавьте адрес в нужную коллекцию.',
          action_en='Relax the filters (country, protocol, hosting) or add the address to the right collection.'),
    _code('SCOPE_DENYLIST', 'scope', 'STATE', params=('reason',), title_ru='Адрес в локальном стоп-листе',
          title_en='Address is on the local denylist',
          action_ru='Уберите адрес из стоп-листа, если исключение больше не нужно: отзыв применяется до замеров.',
          action_en='Remove the address from the denylist if the exception is no longer needed; the deny applies before probes.'),
    _code('SCOPE_REPUTATION_LISTED', 'scope', 'STATE', params=('zone',), title_ru='Адрес числится в DNSBL',
          title_en='Address is listed in a DNSBL',
          action_ru='Это состояние адреса, а не сбой проверки. Дождитесь снятия листинга или отключите строгий режим.',
          action_en='That is an address state, not a check failure. Wait for the listing to clear or relax the strict mode.'),
    _code('SCOPE_FILTERED_ALL', 'export', 'STATE', params=('stage', 'count'), title_ru='Проверку прошли все, в выдачу не попал никто',
          title_en='Everything passed the check, nothing reached the export',
          action_ru='Ослабьте фильтры выдачи (страна, latency, запрос в строке, hosting) или снимите deny для этой выборки.',
          action_en='Relax the export filters (country, latency, query, hosting) or drop the deny for this selection.'),
    _code('NO_PROXIES', 'export', 'STATE', title_ru='В выдаче нет ни одного адреса',
          title_en='The export holds no address',
          action_ru='Проверьте счётчики воронки: ниже указана стадия, на которой адреса терялись.',
          action_en='Check the funnel counters: the stage where addresses were lost is named below.'),
    _code('SCOPE_EMPTY', 'scope', 'STATE', params=('filter',), title_ru='В область проверки не попал ни один адрес',
          title_en='No address is inside the check scope',
          action_ru='Снимите фильтры или добавьте кандидатов: сейчас в коллекции под фильтр не подходит ни один адрес.',
          action_en='Drop the filters or add candidates: right now no address in the collection matches.'),
    _code('NO_SOURCES', 'source', 'STATE', title_ru='Источники не дали ни одного кандидата',
          title_en='Sources produced no candidate',
          action_ru='Добавьте или включите источник, затем повторите сбор; список кандидатов остаётся пустым до этого.',
          action_en='Add or enable a source and collect again; the candidate list stays empty until then.'),
    _code('SOURCE_UNAVAILABLE', 'download', 'UPSTREAM', params=('count',), title_ru='Источники не скачались',
          title_en='Sources could not be downloaded',
          action_ru='Проверьте сеть и адрес источника; последние удачные данные источника не выбрасываются.',
          action_en='Check the network and the source URL; the last good source data is kept.'),
    _code('SOURCE_EMPTY', 'parser', 'UPSTREAM', title_ru='Источник ответил без адресов',
          title_en='Source answered without addresses',
          action_ru='Формат ответа изменился или список вычеркнут. Обновите адаптер источника.',
          action_en='The response format changed or the list was withdrawn. Update the source adapter.'),
    _code('INVALID_PROXY', 'parser', 'UPSTREAM', params=('reason',), title_ru='Строка не похожа на адрес',
          title_en='Line does not look like an address',
          action_ru='Проверьте формат источника; такие строки отбрасываются и не попадают в проверку.',
          action_en='Check the source format; such lines are dropped and never checked.'),
    _code('SOURCE_TOO_LARGE', 'download', 'UPSTREAM', params=('max_bytes',), title_ru='Источник больше лимита',
          title_en='Source larger than the limit',
          action_ru='Поднимите лимит байтов источника или включите постраничную загрузку.',
          action_en='Raise the source byte budget or enable paginated fetching.'),
    _code('SOURCE_CANDIDATE_LIMIT', 'parser', 'UPSTREAM', params=('limit',), title_ru='Источник дал больше кандидатов, чем разрешено',
          title_en='Source produced more candidates than allowed',
          action_ru='Поднимите лимит кандидатов или возьмите источник поменьше: лишние адреса не попадут в проверку.',
          action_en='Raise the candidate limit or pick a smaller source; the rest will not be checked.'),
    _code('SOURCE_LINE_TOO_LARGE', 'parser', 'UPSTREAM', params=('max_line_bytes',), title_ru='Строка источника больше лимита',
          title_en='Source line larger than the limit',
          action_ru='Источник отдаёт неожиданно длинные строки. Ограничьте байты на строку или возьмите другой источник.',
          action_en='The source returns unexpectedly long lines. Cap the bytes per line or use another source.'),
    _code('SOURCE_PRIVATE_DESTINATION', 'download', 'UPSTREAM', params=('host',), title_ru='Источник указывает на служебный адрес',
          title_en='Source points at a private or metadata address',
          action_ru='Публичный сбор не ходит во внутренние адреса. Уберите такой источник или включите доверенный режим явно.',
          action_en='Public collection does not reach internal addresses. Drop such a source or enable the trusted mode explicitly.'),
    _code('SOURCE_REDIRECT_*', 'download', 'UPSTREAM', params=('url',), title_ru='Источник перенаправляет запрос',
          title_en='Source redirects the request',
          action_ru='Укажите в источнике конечный адрес: цепочка редиректов ограничена и понижение http запрещено.',
          action_en='Put the final URL in the source: the redirect chain is bounded and an http downgrade is refused.'),
    _code('SOURCE_INVALID_UTF8', 'parser', 'UPSTREAM', title_ru='Источник не в UTF-8',
          title_en='Source is not UTF-8',
          action_ru='Перекодируйте источник или используйте текстовый адаптер, который ищет адреса в любой кодировке.',
          action_en='Re-encode the source or use the text adapter that finds addresses in any encoding.'),
    _code('DEVICE_NETWORK_DOWN', 'device_network', 'UPSTREAM', title_ru='Нет сети на этом устройстве',
          title_en='This device has no network',
          action_ru='Подключите сеть и повторите. Адреса не признаны негодными: сеть была недоступна у вас, а не у них.',
          action_en='Reconnect the network and retry. No address is judged bad: the network was down here, not there.'),
    _code('DEVICE_DNS_FAILURE', 'dns', 'UPSTREAM', title_ru='Устройство не разрешает имена',
          title_en='This device cannot resolve names',
          action_ru='Проверьте DNS-настройки и VPN: без разрешения имён ни один адрес не будет измерен.',
          action_en='Check DNS settings and the VPN: without name resolution nothing can be measured.'),
    _code('TARGET_OUTAGE', 'target', 'UPSTREAM', title_ru='Цель недоступна для всех адресов',
          title_en='The target is down for every address',
          action_ru='Смените цель или повторите позже. Это не ошибка адресов и не повод снижать их репутацию.',
          action_en='Change the target or retry later. This is not an address error and must not lower their reputation.'),
    _code('TTL_EXPIRED', 'freshness', 'TIME', params=('age_s',), title_ru='Результат устарел',
          title_en='Result expired',
          action_ru='Запустите перепроверку или увеличьте max-age в ревизии профиля, если это допустимо.',
          action_en='Run a recheck or raise max-age in the profile revision if that is acceptable.'),
    _code('TIME_UNKNOWN', 'freshness', 'TIME', params=('field',), title_ru='Время измерения неизвестно',
          title_en='Measurement time is unknown',
          action_ru='Строка без времени измерения не доказывает пригодность. Переизмерьте адрес.',
          action_en='A row without a measurement time proves nothing. Measure it again.'),
    _code('TIME_FUTURE', 'freshness', 'TIME', params=('delta_s',), title_ru='Время измерения из будущего',
          title_en='Measurement time is in the future',
          action_ru='Сверьте часы устройства. Подозрительное время не продлевает срок строки.',
          action_en='Check the device clock. A suspicious time does not extend the row lifetime.'),
    _code('CLOCK_ROLLBACK', 'freshness', 'TIME', params=('delta_s',), title_ru='Часы устройства откатились назад',
          title_en='The device clock rolled back',
          action_ru='Время ушло назад, поэтому прежнее измерение недействительно. Измерьте заново после исправления часов.',
          action_en='Time moved backwards, so the old measurement is void. Measure again after fixing the clock.'),
    _code('JOB_FAILED', 'export', 'STATE', params=('stop_reason',), title_ru='Задание завершилось ошибкой',
          title_en='The job ended with an error',
          action_ru='Откройте журнал задания и повторите запуск; собранные данные при этом не теряются.',
          action_en='Open the job log and run it again; collected data is not lost.'),
    _code('VERSION_UNKNOWN', 'environment', 'UPSTREAM', title_ru='Версия программы неизвестна',
          title_en='Program version is unknown',
          action_ru='Соберите версию вручную (proxy-workbench --version) и приложите её к сообщению.',
          action_en='Record the version by hand (proxy-workbench --version) and attach it to the report.'),
    _code('SCHEMA_UNKNOWN', 'environment', 'DATA', params=('user_version',), title_ru='Версия схемы базы неизвестна',
          title_en='Database schema version is unknown',
          action_ru='Проверьте, что база открыта этой же версией программы, и не редактируйте её сторонними программами.',
          action_en='Check that the database is opened by the same program version and is not edited by other tools.'),
    _code('SCOPE_UNSPECIFIED', 'scope', 'STATE', params=('field',), title_ru='Область проверки не зафиксирована',
          title_en='The check scope is not pinned',
          action_ru='Укажите коллекцию, профиль и ревизию: без них результат нельзя объяснить и повторить.',
          action_en='Pin the collection, profile and revision: without them a result cannot be explained or repeated.'),
    _code('RECIPE_NO_PROFILE', 'environment', 'STATE', title_ru='Не хватает профиля для повторения замера',
          title_en='No profile to reproduce the measurement',
          action_ru='Передайте конфигурацию профиля, иначе цель, таймауты и условия неизвестны.',
          action_en='Pass the profile configuration, otherwise target, timeouts and assertions are unknown.'),
    _code('RECIPE_NO_SAMPLES', 'environment', 'STATE', title_ru='У строки нет образцов замера',
          title_en='The row carries no samples',
          action_ru='Нужен список образцов (статус и код по каждой попытке), чтобы повторить замер честно.',
          action_en='A sample list (status and code per attempt) is required to reproduce a measurement honestly.'),
)

# The canonical `E_<DOMAIN>_<REASON>` codes of CONTRACTS §5.4 that the diagnostics
# layer can itself produce.  Codes owned by other modules (keys, gateway, db
# migrations) are documented in the contract and are not duplicated here.
_register(
    _code('E_AUTH_RATE_LIMITED', 'rate_limit', 'AUTH', params=('retry_after_s',),
          title_ru='Ключ или клиент превысил лимит запросов',
          title_en='Key or client exceeded the request rate',
          action_ru='Подождите Retry-After и снизьте частоту запросов к API.',
          action_en='Wait for Retry-After and lower the request rate to the API.'),
    _code('E_LIMIT_BUDGET', 'budget', 'LIMIT', params=('kind', 'used', 'limit'),
          title_ru='Бюджет задания исчерпан',
          title_en='The job budget is exhausted',
          action_ru='Поднимите бюджет запросов, байтов или времени. Бюджет 0 останавливает работу без обхода политики.',
          action_en='Raise the request, byte or time budget. A zero budget stops the run without bypassing policy.'),
    _code('E_LIMIT_CONCURRENCY', 'budget', 'LIMIT', params=('limit',),
          title_ru='Достигнут предел параллелизма',
          title_en='The concurrency limit is reached',
          action_ru='Уменьшите число одновременных проверок или увеличьте предел, если система это тянет.',
          action_en='Lower the number of parallel checks or raise the limit if the machine can take it.'),
    _code('E_TIME_TTL_EXPIRED', 'freshness', 'TIME', params=('age_s',),
          title_ru='Срок строки истёк',
          title_en='The row lifetime expired',
          action_ru='Истёкшие строки ждут перепроверки; увеличьте max-age или запустите перепроверку.',
          action_en='Expired rows await a recheck; raise max-age or run a recheck.'),
    _code('E_TIME_UNKNOWN', 'freshness', 'TIME', params=('field',),
          title_ru='Время строки неизвестно',
          title_en='The row time is unknown',
          action_ru='Строка без измеренного времени не доказывает пригодность и требует переизмерения.',
          action_en='A row without a measured time proves nothing and must be measured again.'),
    _code('E_TIME_FUTURE', 'freshness', 'TIME', params=('delta_s',),
          title_ru='Время строки из будущего',
          title_en='The row time is in the future',
          action_ru='Сверьте часы устройства и переизмерьте: подозрительное время не считается свежим.',
          action_en='Check the device clock and measure again; a suspicious time is never counted as fresh.'),
    _code('E_TIME_CLOCK_ROLLBACK', 'freshness', 'TIME', params=('delta_s',),
          title_ru='Часы откатились назад',
          title_en='The clock rolled back',
          action_ru='Прежнее измерение недействительно после отката часов. Исправьте часы и измерьте заново.',
          action_en='A previous measurement is void after a clock rollback. Fix the clock and measure again.'),
    _code('E_TIME_TTL_MISSING', 'freshness', 'TIME', params=('field',),
          title_ru='У строки нет записанного срока',
          title_en='The row records no lifetime',
          action_ru='Новая строка без срока несостоятельна: измерьте адрес заново. Для legacy-строк срок восстанавливается один раз.',
          action_en='A new row without a recorded lifetime is inconsistent: measure it again. A legacy row gets one backfilled deadline.'),
    _code('E_STATE_SNAPSHOT_STALE', 'freshness', 'STATE', title_ru='Снимок истёк',
          title_en='The snapshot is stale',
          action_ru='Опубликуйте новый снимок: истёкший нельзя выдать за актуальный, но свежие строки в нём остаются.',
          action_en='Publish a new snapshot: an expired one must not be served as current, while its fresh rows stay.'),
    _code('E_STATE_NO_SNAPSHOT', 'export', 'STATE', title_ru='Нет опубликованного снимка',
          title_en='No published snapshot',
          action_ru='Запустите проверку и опубликуйте набор; потребители не переходят на выдачу молча.',
          action_en='Run a check and publish a set; consumers do not switch output silently.'),
    _code('E_SECRET_VAULT_LOCKED', 'auth', 'SECRET', title_ru='Хранилище секретов закрыто',
          title_en='Secret store is locked',
          action_ru='Откройте хранилище и повторите: без доступа пароль не подставляется и адрес не проходит.',
          action_en='Unlock the store and retry: without access no password is substituted and the address cannot pass.'),
    _code('E_SECRET_NOT_PROVIDED', 'auth', 'SECRET', title_ru='Учётные данные не заданы',
          title_en='No credentials provided',
          action_ru='Задайте доступ к адресу: без логина и пароля прокси с авторизацией не пройдёт.',
          action_en='Set the access for the address: without login and password an authenticating proxy cannot pass.'),
    _code('E_IMPORT_FORMAT', 'parser', 'IMPORT', params=('source', 'line',),
          title_ru='Файл или источник не распознан',
          title_en='File or source not recognised',
          action_ru='Проверьте формат: TXT, CSV, JSON или URI. Строка с ошибкой видна в отчёте импорта.',
          action_en='Check the format: TXT, CSV, JSON or URI. The failing line is visible in the import report.'),
    _code('E_STATE_POOL_EMPTY', 'export', 'STATE', params=('desired',),
          title_ru='Пул пуст',
          title_en='The pool is empty',
          action_ru='Недобор показан числом и причиной. Прямой обход запрещён: пополняйте пул из резерва и источников.',
          action_en='The shortfall is shown with a number and a reason. A direct fallback is forbidden: refill from reserve and sources.'),
)

#: Every `E_<DOMAIN>_<REASON>` code the contract fixes (CONTRACTS §5.4).  The
#: diagnostics layer may describe any of them, but it must not mint a new one:
#: codes owned by keys, gateway or migrations stay where the contract puts them.
CONTRACT_CODES = (
    'E_AUTH_MISSING', 'E_AUTH_INVALID', 'E_AUTH_EXPIRED', 'E_AUTH_REVOKED', 'E_AUTH_SCOPE',
    'E_AUTH_PERMISSION', 'E_AUTH_RATE_LIMITED', 'E_AUTH_ROTATION_GRACE',
    'E_VALIDATION_SCHEMA', 'E_VALIDATION_FIELD', 'E_VALIDATION_UNKNOWN_FIELD',
    'E_CONFLICT_REVISION', 'E_CONFLICT_IDEMPOTENCY', 'E_CONFLICT_BUSY',
    'E_DATA_DB_VERSION_AHEAD', 'E_DATA_DB_FOREIGN', 'E_DATA_MIGRATION_FAILED', 'E_DATA_BACKUP_FAILED',
    'E_IMPORT_REVISION', 'E_IMPORT_FORMAT', 'E_IMPORT_PARTIAL',
    'E_SECRET_VAULT_LOCKED', 'E_SECRET_UPSTREAM_AUTH_REQUIRED', 'E_SECRET_UPSTREAM_AUTH_FAILED',
    'E_SECRET_NOT_PROVIDED',
    'E_LIMIT_BODY', 'E_LIMIT_QUEUE', 'E_LIMIT_CONCURRENCY', 'E_LIMIT_BUDGET',
    'E_STATE_NO_SNAPSHOT', 'E_STATE_SNAPSHOT_STALE', 'E_STATE_SNAPSHOT_SCHEMA', 'E_STATE_NO_PROXIES',
    'E_STATE_JOB_RUNNING', 'E_STATE_POOL_EMPTY',
    'E_TIME_UNKNOWN', 'E_TIME_FUTURE', 'E_TIME_CLOCK_ROLLBACK', 'E_TIME_TTL_EXPIRED',
    'E_TIME_TTL_MISSING',
    'E_GATEWAY_NO_UPSTREAM', 'E_GATEWAY_DEADLINE', 'E_GATEWAY_SLOT_UNAVAILABLE',
    'E_GATEWAY_TRANSPORT_UNSUPPORTED',
)

CODE_STAGES: dict[str, str] = {code: entry.stage for code, entry in CODES.items()}

#: Exception class names produced by the engine today, mapped to the code that
#: says what actually happened.  CONTRACTS §5.4 records that ``ConnectError``,
#: ``ConnectTimeout``, ``ReadTimeout``, ``ProxyError`` and ``SSLError`` used to
#: collapse into one value; this table is what separates them.
EXCEPTION_CODES: dict[str, tuple[str, str]] = {
    'ConnectTimeout': ('tcp', 'TCP_TIMEOUT'),
    'ConnectError': ('tcp', 'UNREACHABLE'),
    'ConnectionRefusedError': ('tcp', 'TCP_REFUSED'),
    'ConnectionResetError': ('tcp', 'TCP_RESET'),
    'ConnectionAbortedError': ('tcp', 'TCP_RESET'),
    'ReadTimeout': ('target', 'TIMEOUT'),
    'WriteTimeout': ('target', 'TIMEOUT'),
    'PoolTimeout': ('target', 'TIMEOUT'),
    'TimeoutException': ('target', 'TIMEOUT'),
    'TimeoutError': ('target', 'TIMEOUT'),
    'ProxyError': ('handshake', 'HANDSHAKE_ERROR'),
    'RemoteProtocolError': ('handshake', 'HANDSHAKE_ERROR'),
    'LocalProtocolError': ('handshake', 'HANDSHAKE_ERROR'),
    'SSLError': ('tls', 'TLS_ERROR'),
    'SSLCertVerificationError': ('tls', 'TLS_CERT_INVALID'),
    'CertificateError': ('tls', 'TLS_CERT_INVALID'),
    'TooManyRedirects': ('target', 'HTTP_3XX'),
    'IncompleteRead': ('target', 'CONTENT_TRUNCATED'),
    'UnicodeDecodeError': ('parser', 'SOURCE_INVALID_UTF8'),
    'UnicodeError': ('parser', 'SOURCE_INVALID_UTF8'),
    'OSError': ('tcp', 'UNREACHABLE'),
}

#: Substrings that turn a generic connect failure into a DNS diagnosis.  httpx
#: wraps resolver errors in ``ConnectError``, so the message is the only signal.
DNS_MARKERS = (
    'name or service not known', 'nodename nor servname', 'temporary failure in name resolution',
    'name resolution', 'no address associated with hostname', 'no such host',
    'could not resolve', 'getaddrinfo failed',
)

#: Well-known addresses for the device control checks.  The control never needs
#: an answer, only that the network and the resolver work at all.
CONTROL_RESOLVE_HOST = 'example.com'
CONTROL_CONNECT_ADDRESS = ('1.1.1.1', 443)
CONTROL_CONNECT_REASON = 'Устройство не может выйти в интернет: сеть или файрвол блокируют исходящие соединения.'

#: Exception classes this module raises.  Useful errors carry an action, a code
#: and the parameters of the code, not only a class name (F25).
class ActionableError(Exception):
    """An error that carries a documented code and a recovery action."""

    def __init__(self, code: str, *, params: Mapping[str, Any] | None = None, cause: BaseException | None = None):
        if code not in CODES:
            raise KeyError(f'undocumented error code: {code}')
        self.code = code
        self.params = dict(params or {})
        self.cause = cause
        super().__init__(code)

    @property
    def stage(self) -> str:
        return CODES[self.code].stage

    @property
    def help(self) -> 'ErrorHelp':
        return help_for(self.code, params=self.params)

    def action(self, lang: str | None = None) -> str:
        return help_for(self.code, params=self.params, lang=lang).action

    def render(self, lang: str | None = None) -> str:
        return help_for(self.code, params=self.params, lang=lang).render(lang)


def actionable(code: str, *, params: Mapping[str, Any] | None = None, cause: BaseException | None = None) -> ActionableError:
    """Build an `ActionableError` without raising it."""
    return ActionableError(code, params=params, cause=cause)


def is_known_code(code: Any) -> bool:
    return isinstance(code, str) and code in CODES


@dataclasses.dataclass(frozen=True)
class ErrorHelp:
    """Localized help for one code: what it is, and what to do about it."""

    code: str
    stage: str
    domain: str
    known: bool
    title: str
    action: str
    params: tuple[str, ...] = ()
    values: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    def render(self, lang: str | None = None) -> str:
        prefix = 'Действие' if _pick('ru', 'en', lang) == 'ru' else 'Action'
        if self.values:
            shown = ', '.join(f'{key}={self.values[key]}' for key in sorted(self.values))
            return f'{self.title} ({self.code}; {shown})\n{prefix}: {self.action}'
        return f'{self.title} ({self.code})\n{prefix}: {self.action}'

    def to_dict(self) -> dict[str, Any]:
        return {'code': self.code, 'stage': self.stage, 'domain': self.domain, 'known': self.known,
                'title': self.title, 'action': self.action, 'params': list(self.params),
                'values': dict(self.values)}


def help_for(code: Any, *, params: Mapping[str, Any] | None = None, lang: str | None = None) -> ErrorHelp:
    """Localized help for a code.  An undocumented code gets honest generic text."""
    entry = CODES.get(code) if isinstance(code, str) else None
    values = {key: value for key, value in dict(params or {}).items() if value is not None}
    if entry is None:
        unknown_ru = f'Неизвестный код ошибки: {code}'
        unknown_en = f'Unknown error code: {code}'
        action_ru = 'Код не описан в справочнике. Пришлите его вместе с журналом задания.'
        action_en = 'This code is not in the reference. Send it together with the job log.'
        return ErrorHelp(str(code), '', 'UNKNOWN', False,
                         _pick(unknown_ru, unknown_en, lang),
                         _pick(action_ru, action_en, lang), (), values)
    return ErrorHelp(entry.code, entry.stage, entry.domain, True, entry.title(lang), entry.action(lang),
                     entry.params, values)


def help_entries() -> tuple[ErrorHelp, ...]:
    """Every documented code, in funnel order, for an in-app help page."""
    return tuple(help_for(code) for code in sorted(CODES, key=lambda item: (STAGES.index(CODE_STAGES[item]), item)))


def codes_for_stage(stage: str) -> tuple[str, ...]:
    if stage not in STAGES:
        raise KeyError(f'unknown stage: {stage}')
    return tuple(code for code, code_stage in CODE_STAGES.items() if code_stage == stage)


def code_stage(code: str) -> str:
    if code not in CODES:
        raise KeyError(f'undocumented error code: {code}')
    return CODE_STAGES[code]


def code_title(code: str, lang: str | None = None) -> str:
    return help_for(code, lang=lang).title


def code_action(code: str, lang: str | None = None) -> str:
    return help_for(code, lang=lang).action


def stage_of(value: Any) -> str:
    """The stage a failure belongs to, without losing the code itself."""
    entry = value if isinstance(value, Code) else CODES.get(value)
    if entry is not None:
        return entry.stage
    return classification(value)[0]


def classification(value: Any) -> tuple[str, str]:
    """Map any engine error value to ``(stage, code)``.

    Accepts a code, a bare marker, an exception instance, an exception class or
    ``None``.  An unknown value is never dropped silently: it becomes
    ``UNKNOWN_FAILURE`` on the stage it was seen at, so a new engine literal
    shows up in the funnel instead of disappearing.
    """
    if value is None:
        return '', ''
    if isinstance(value, Code):
        return value.stage, value.code
    if isinstance(value, type) and issubclass(value, BaseException):
        return _exception_classification(value, '')
    if isinstance(value, BaseException):
        return _exception_classification(type(value), str(value))
    if isinstance(value, Mapping):
        named = value.get('code') or value.get('error')
        if named:
            return classification(named)
        status = value.get('status')
        if _finite_number(status):
            return classification(f'HTTP_{int(status)}')
        return '', ''
    text = str(value).strip()
    if not text:
        return '', ''
    if text in CODES:
        return CODES[text].stage, text
    if text in EXCEPTION_CODES:
        return EXCEPTION_CODES[text]
    lowered = text.lower()
    if any(marker in lowered for marker in DNS_MARKERS):
        return 'dns', 'DNS_ERROR'
    status_match = re.fullmatch(r'HTTP_(\d{3})', text)
    if status_match:
        status = int(status_match.group(1))
        # Only the codes that name a cause unambiguously move to another stage;
        # 403 stays a target response because a block page is not an auth problem.
        if status == 429:
            return 'rate_limit', 'RATE_LIMITED'
        if status in (401, 407):
            return 'auth', 'AUTH_FAILED'
        return 'target', text
    if re.fullmatch(r'HTTP_(3XX|4XX|5XX)', text):
        return 'target', text
    if re.fullmatch(r'SOURCE_[A-Z0-9_]+', text):
        return _source_stage(text)
    return 'target', 'UNKNOWN_FAILURE'


def _source_stage(text: str) -> tuple[str, str]:
    if text in CODES:
        return CODES[text].stage, text
    # A candidate budget is spent while parsing, everything else while fetching.
    if text.endswith('_LIMIT') or text.endswith('_TOO_LARGE') or text.endswith('_UTF8'):
        return 'parser', text
    return 'download', text


def _exception_classification(exc_type: type, message: str) -> tuple[str, str]:
    mapped: tuple[str, str] | None = None
    for name in getattr(exc_type, '__mro__', (exc_type,)):
        key = getattr(name, '__name__', str(name))
        if key in EXCEPTION_CODES:
            mapped = EXCEPTION_CODES[key]
            break
    if mapped is None:
        return 'target', 'UNKNOWN_FAILURE'
    # Every network stack reports a resolver failure as a connect failure, so a
    # resolver message promotes a connection error to a DNS diagnosis.
    if mapped[0] in ('tcp', 'dns', 'device_network'):
        lowered = (message or '').lower()
        if any(marker in lowered for marker in DNS_MARKERS):
            return 'dns', 'DNS_ERROR'
    return mapped


def explain_error(value: Any, *, lang: str | None = None, params: Mapping[str, Any] | None = None) -> ErrorHelp:
    """Localized help for an exception, a code or a raw engine error value."""
    _, code = classification(value)
    return help_for(code or 'UNKNOWN_FAILURE', params=params, lang=lang)


# --- the funnel ---------------------------------------------------------------

@dataclasses.dataclass
class StageCounters:
    """How many items entered a stage and how many were lost there."""

    entered: int = 0
    lost: int = 0
    reasons: Counter = dataclasses.field(default_factory=Counter)
    attribution: Counter = dataclasses.field(default_factory=Counter)

    def enter(self, count: int = 1) -> None:
        if count > 0:
            self.entered += count

    def drop(self, code: str, count: int = 1, attribution: str = 'proxy') -> None:
        if count <= 0:
            return
        self.lost += count
        self.reasons[code] += count
        self.attribution[attribution] += count

    def to_dict(self) -> dict[str, Any]:
        return {'entered': self.entered, 'lost': self.lost,
                'reasons': dict(sorted(self.reasons.items())),
                'attribution': dict(sorted(self.attribution.items()))}


@dataclasses.dataclass(frozen=True)
class SourceReport:
    """The shape `collect()` already returns for one source."""

    index: Any = None
    rows: int = 0
    invalid: int = 0
    blocked: int = 0
    complete: bool = False
    error: str | None = None
    format: str | None = None

    @classmethod
    def from_mapping(cls, report: Mapping[str, Any]) -> 'SourceReport':
        return cls(index=report.get('source', report.get('input')),
                   rows=int(report.get('rows') or 0),
                   invalid=int(report.get('invalid') or 0),
                   blocked=int(report.get('blocked') or 0),
                   complete=bool(report.get('complete')),
                   error=report.get('error') or None,
                   format=report.get('format'))


def record_source_report(report: Mapping[str, Any] | SourceReport,
                         counters: dict[str, Any] | None = None) -> 'SourceReport':
    """Record one source fetch into the collect counters.

    Download failures and parser failures land on different stages, which is
    the first half of F10: "no proxies because the source was unreachable" and
    "no proxies because nothing parsed" need different actions.
    """
    entry = report if isinstance(report, SourceReport) else SourceReport.from_mapping(report)
    if counters is not None:
        counters.setdefault('rows', 0)
        counters['rows'] += entry.rows
        counters['invalid'] = counters.get('invalid', 0) + entry.invalid
        counters['blocked'] = counters.get('blocked', 0) + entry.blocked
        errors = counters.setdefault('errors', Counter())
        stages = counters.setdefault('stages', Counter())
        if entry.error:
            stage, code = classification(entry.error)
            errors[code] += 1
            stages[stage] += 1
        elif entry.rows == 0:
            errors['SOURCE_EMPTY'] += 1
            stages['parser'] += 1
    return entry


@dataclasses.dataclass
class Funnel:
    """Counters of a run: what entered each stage, what was lost and why."""

    stages: dict[str, StageCounters] = dataclasses.field(default_factory=dict)
    terminal: Counter = dataclasses.field(default_factory=Counter)
    verdicts: Counter = dataclasses.field(default_factory=Counter)
    attribution: Counter = dataclasses.field(default_factory=Counter)
    totals: dict[str, Any] = dataclasses.field(default_factory=dict)
    sources: tuple[SourceReport, ...] = ()
    control: 'ControlVerdict | None' = None
    set_state: str = ''
    stop_reason: str = ''

    def counters(self, stage: str) -> StageCounters:
        if stage not in STAGES:
            raise KeyError(f'unknown stage: {stage}')
        return self.stages.setdefault(stage, StageCounters())

    def enter(self, stage: str, count: int = 1) -> None:
        self.counters(stage).enter(count)

    def drop(self, stage: str, code: str, count: int = 1, attribution: str = 'proxy') -> None:
        self.counters(stage).drop(code, count, attribution)
        self.attribution[attribution] += count

    def finish(self, state: str, count: int = 1) -> None:
        self.terminal[state] += count

    def entry(self) -> int:
        """How many proxies entered the measurement chain at `scope`."""
        counters = self.stages.get('scope')
        return counters.entered if counters is not None else 0

    def source_entry(self) -> int:
        """How many sources entered the collect chain."""
        counters = self.stages.get('source')
        return counters.entered if counters is not None else 0

    def stage_counters(self, stage: str) -> StageCounters:
        return self.stages.get(stage, StageCounters())

    def lost_total(self) -> int:
        return sum(counters.lost for counters in self.stages.values())

    def surviving(self, stage: str) -> int:
        """How many items got past `stage` without being lost."""
        counters = self.stages.get(stage)
        if counters is None:
            return 0
        return counters.entered - counters.lost

    def losses(self) -> tuple[tuple[str, str, int], ...]:
        """``(stage, code, count)`` for every loss, biggest first."""
        items = [(stage, code, count)
                 for stage, counters in self.stages.items()
                 for code, count in counters.reasons.items()]
        order = {stage: position for position, stage in enumerate(STAGES)}
        return tuple(sorted(items, key=lambda item: (-item[2], order.get(item[0], 99), item[1])))

    def dominant(self) -> tuple[str, str, int]:
        losses = self.losses()
        return losses[0] if losses else ('', '', 0)

    def reason(self) -> str:
        return self.dominant()[1]

    def zero_result(self) -> bool:
        exported = self.totals.get('exported')
        if _finite_number(exported):
            return float(exported) <= 0
        return self.terminal.get(TIME_OK, 0) <= 0 and self.lost_total() <= 0

    def to_dict(self) -> dict[str, Any]:
        return {
            'stages': {stage: counters.to_dict()
                       for stage, counters in self.stages.items() if counters.entered or counters.lost},
            'terminal': dict(sorted(self.terminal.items())),
            'verdicts': dict(sorted(self.verdicts.items())),
            'attribution': dict(sorted(self.attribution.items())),
            'totals': dict(self.totals),
            'set_state': self.set_state,
            'stop_reason': self.stop_reason,
            'control': self.control.state if self.control is not None else None,
        }


def _time_state(row: Mapping[str, Any], now: float, last_seen: Mapping[str, Any] | None, *,
                backfill_legacy_ttl: bool = True, future_tolerance_s: float = CLOCK_SKEW_SECONDS,
                legacy_max_age_s: float = LEGACY_MAX_AGE_SECONDS) -> tuple[str, str | None]:
    """The time state of one row (CONTRACTS §2.4), plus its code when not ok.

    The state names are the same strings as `core.TIME_STATES`, so the funnel
    and the admission contract cannot drift apart; `core` stays the authority
    and this function is the funnel's own view of the same axis.
    """
    proxy = row.get('proxy')
    if last_seen is not None and proxy in last_seen:
        previous = last_seen[proxy]
        if _finite_number(previous) and now < float(previous):
            return CLOCK_ROLLBACK, 'CLOCK_ROLLBACK'
    checked_at = row.get('checked_at')
    if not _finite_number(checked_at) or float(checked_at) <= 0:
        return TIME_UNKNOWN, 'TIME_UNKNOWN'
    checked_at = float(checked_at)
    if checked_at > now + future_tolerance_s:
        return TIME_FUTURE, 'TIME_FUTURE'
    valid_until = row.get('valid_until')
    if valid_until is None:
        if not backfill_legacy_ttl:
            return TIME_TTL_MISSING, 'E_TIME_TTL_MISSING'
        # Legacy row: one synthetic lifetime, never endless freshness.
        valid_until = checked_at + legacy_max_age_s
    if not _finite_number(valid_until) or float(valid_until) <= 0:
        return TIME_UNKNOWN, 'TIME_UNKNOWN'
    return (TIME_EXPIRED if float(valid_until) <= now else TIME_OK), None


def data_state(row: Mapping[str, Any], now: float, *, last_seen: Mapping[str, Any] | None = None,
               backfill_legacy_ttl: bool = True, future_tolerance_s: float = CLOCK_SKEW_SECONDS,
               legacy_max_age_s: float = LEGACY_MAX_AGE_SECONDS) -> str:
    """`time_ok` / `time_expired` / `time_unknown` / `time_future` / `clock_rollback` / `time_ttl_missing`."""
    return _time_state(row, now, last_seen, backfill_legacy_ttl=backfill_legacy_ttl,
                       future_tolerance_s=future_tolerance_s, legacy_max_age_s=legacy_max_age_s)[0]


def _blocked(row: Mapping[str, Any]) -> tuple[str, str] | None:
    """A policy block decided before any measurement (CONTRACTS §6.3)."""
    verdict = row.get('reputation')
    if not isinstance(verdict, Mapping):
        return None
    if verdict.get('local_rule') is not None or verdict.get('status') == 'local_denied':
        return 'scope', 'SCOPE_DENYLIST'
    for zone in verdict.get('dnsbl') or ():
        if isinstance(zone, Mapping) and zone.get('status') == 'listed':
            return 'scope', 'SCOPE_REPUTATION_LISTED'
    return None


def _sample_stage(sample: Mapping[str, Any]) -> tuple[str, str]:
    error = sample.get('error')
    if error:
        stage, code = classification(error)
        status = sample.get('status')
        if not code or code == 'UNKNOWN_FAILURE':
            if _finite_number(status):
                return 'target', f'HTTP_{int(status) // 100}XX'
            return 'target', 'UNKNOWN_FAILURE'
        return stage, code
    if sample.get('ok'):
        return '', ''
    return 'target', 'UNKNOWN_FAILURE'


def _row_loss(row: Mapping[str, Any]) -> tuple[str, str] | None:
    """The first stage at which a row dropped out, or None when it survived."""
    blocked = _blocked(row)
    if blocked is not None:
        return blocked
    samples = [sample for sample in (row.get('samples') or ()) if isinstance(sample, Mapping)]
    if not samples and row.get('error'):
        stage, code = classification(row['error'])
        return stage, code or 'UNKNOWN_FAILURE'
    for sample in samples:
        stage, code = _sample_stage(sample)
        if code:
            return stage, code
    return None


def _row_measured(row: Mapping[str, Any]) -> bool:
    """Whether the row carries a measurement at all, not only a verdict."""
    if any(isinstance(sample, Mapping) for sample in (row.get('samples') or ())):
        return True
    return bool(row.get('requests'))


def _row_unknown_verdict(row: Mapping[str, Any]) -> str | None:
    """The documented code behind a verdict that stayed unknown, without guessing."""
    for source in (row.get('reputation'), row.get('anonymity')):
        if not isinstance(source, Mapping):
            continue
        error = source.get('error')
        level = source.get('status', source.get('level'))
        if not error or level not in (None, 'unknown'):
            continue
        _stage, code = classification(error)
        if is_known_code(code):
            return code
    return None


def build_funnel(rows: Iterable[Mapping[str, Any]] = (), *, status: Mapping[str, Any] | None = None,
                 sources: Iterable[Mapping[str, Any] | SourceReport] = (),
                 control: 'ControlVerdict | None' = None, admitted: Any = None,
                 now: float | None = None, last_seen: Mapping[str, Any] | None = None,
                 backfill_legacy_ttl: bool = True, future_tolerance_s: float = CLOCK_SKEW_SECONDS,
                 legacy_max_age_s: float = LEGACY_MAX_AGE_SECONDS) -> Funnel:
    """Build the funnel from stored result rows.

    `rows` are the payloads the engine already stores (`results.payload` /
    the observation of CONTRACTS §2.1): `error`, `samples`, `reputation`,
    `checked_at`, `valid_until`.  `status` is the export `status.json`, `sources`
    the `collect()` reports, `control` an optional device/target control verdict
    and `admitted` the result of the admission contract — `core.admit` bound to
    its scope, access and policy, e.g.
    `admitted=lambda row: core.admit(row, scope, access, policy, now).admitted`.
    """
    now = time.time() if now is None else float(now)
    funnel = Funnel(control=control)
    for stage in FUNNEL_STAGES:
        funnel.counters(stage)
    if isinstance(status, Mapping):
        funnel.set_state = str(status.get('state') or '')
        funnel.stop_reason = str(status.get('stop_reason') or '')
        for key in ('scope_candidates', 'checked', 'pending', 'passed', 'candidates',
                    'local_filtered', 'exported', 'selection_requested', 'selection_exported'):
            if key in status:
                funnel.totals[key] = status[key]
    reports = []
    for report in sources:
        collected: dict[str, Any] = {}
        entry = record_source_report(report, collected)
        reports.append(entry)
        stages = collected.get('stages') or Counter()
        errors = collected.get('errors') or Counter()
        for stage, count in stages.items():
            code = next((name for name in errors if classification(name)[0] == stage), 'SOURCE_UNAVAILABLE')
            funnel.drop(stage, code, count, 'environment' if stage in ENVIRONMENT_STAGES else 'proxy')
    funnel.sources = tuple(reports)
    if reports:
        # The collect chain counts sources and rows, not proxies.
        funnel.enter('source', len(reports))
        funnel.enter('download', sum(1 for report in reports if report.complete))
        funnel.enter('parser', sum(report.rows for report in reports))
    passed = 0
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        funnel.enter('scope', 1)
        loss = _row_loss(row)
        if loss is not None:
            stage, code = loss
            funnel.drop(stage, code, 1, attribution_for(stage, control))
            continue
        state, time_code = _time_state(row, now, last_seen, backfill_legacy_ttl=backfill_legacy_ttl,
                                       future_tolerance_s=future_tolerance_s, legacy_max_age_s=legacy_max_age_s)
        if time_code is not None:
            funnel.drop('freshness', time_code, 1, 'proxy')
            funnel.finish(state)
            continue
        if not _row_measured(row):
            # A row without samples proves nothing; it is not a measurement.
            funnel.finish(OBSERVATION_MISSING)
            continue
        funnel.finish(state)
        unknown = _row_unknown_verdict(row)
        if unknown is not None:
            funnel.verdicts[unknown] += 1
            funnel.finish('unknown_verdict')
        if state == TIME_OK:
            passed += int(_is_admitted(row, admitted))
    if funnel.totals.get('passed') is None:
        funnel.totals['time_ok'] = funnel.terminal.get(TIME_OK, 0)
        if admitted is not None:
            funnel.totals['passed'] = passed
    return funnel


def _is_admitted(row: Mapping[str, Any], admitted: Any) -> bool:
    if admitted is None:
        return True
    if callable(admitted):
        return bool(admitted(row))
    return row.get('proxy') in set(admitted)


def attribution_for(stage: str, control: 'ControlVerdict | None') -> str:
    """`proxy` unless the control proves the fault was ours or the target's."""
    if control is None:
        return 'proxy'
    return control.blame(stage)


# --- why zero results ---------------------------------------------------------

@dataclasses.dataclass
class ZeroResult:
    """A zero-result answer: the code, its counters, and what to do next."""

    code: str
    stage: str
    summary: str
    action: str
    counters: dict[str, Any] = dataclasses.field(default_factory=dict)
    params: dict[str, Any] = dataclasses.field(default_factory=dict)
    related: tuple[str, ...] = ()

    def help(self, lang: str | None = None) -> ErrorHelp:
        return help_for(self.code, params=self.params, lang=lang)

    def render(self, lang: str | None = None) -> str:
        prefix = 'Действие' if _pick('ru', 'en', lang) == 'ru' else 'Action'
        detail = ', '.join(f'{key}={self.counters[key]}' for key in sorted(self.counters)) if self.counters else ''
        line = f'{self.summary} [{self.code}]'
        if detail:
            line += f' ({detail})'
        return f'{line}\n{prefix}: {self.action}'

    def to_dict(self) -> dict[str, Any]:
        return {'code': self.code, 'stage': self.stage, 'summary': self.summary, 'action': self.action,
                'counters': dict(self.counters), 'params': dict(self.params), 'related': list(self.related)}


def _dominant_stage_loss(funnel: Funnel, stages: Sequence[str]) -> tuple[str, int]:
    best_stage, best_count = '', 0
    for stage in stages:
        count = funnel.stages.get(stage, StageCounters()).lost
        if count > best_count:
            best_stage, best_count = stage, count
    return best_stage, best_count


def explain_zero(funnel: Funnel, *, lang: str | None = None) -> ZeroResult | None:
    """Explain an empty result set, or return None when there is something to show.

    The rules are ordered from the outside in: a broken machine, a broken
    source, an exhausted budget, an empty scope, expired or unknown rows, an
    upstream failure, and finally the filters of the export itself.  Only the
    cause that dominates the counters is reported, so the answer stays short
    enough to be read without a traceback.
    """
    if not funnel.zero_result():
        return None
    totals = funnel.totals
    attribution = funnel.attribution
    proxy_lost = attribution.get('proxy', 0)
    device_lost = attribution.get('device', 0)
    target_lost = attribution.get('target', 0)
    control = funnel.control

    def build(code: str, *, params: Mapping[str, Any] | None = None, related: Sequence[str] = ()) -> ZeroResult:
        help_text = help_for(code, params=params, lang=lang)
        return ZeroResult(code=code, stage=help_text.stage, summary=help_text.title, action=help_text.action,
                          counters=dict(funnel.to_dict()['totals']), params=dict(params or {}),
                          related=tuple(related))

    if funnel.stop_reason == 'error' or funnel.set_state == 'error':
        return build('JOB_FAILED', params={'stop_reason': funnel.stop_reason or 'error'})
    if funnel.sources and not any(report.complete for report in funnel.sources) and source_rows(funnel) == 0:
        return build('SOURCE_UNAVAILABLE', params={'count': len(funnel.sources)},
                     related=[report.error for report in funnel.sources if report.error])
    if funnel.sources and all(report.rows == 0 for report in funnel.sources):
        return build('SOURCE_EMPTY', related=('NO_SOURCES',))
    candidates = totals.get('candidates')
    scope_candidates = totals.get('scope_candidates')
    if funnel.entry() <= 0:
        if _finite_number(candidates) and float(candidates) > 0 and (scope_candidates in (None, 0)):
            return build('SCOPE_EMPTY', params={'filter': 'scope'},
                         related=('SCOPE_FILTERED', 'SCOPE_DENYLIST', 'SCOPE_REPUTATION_LISTED'))
        if funnel.source_entry() == 0 and (candidates in (None, 0)) and funnel.lost_total() == 0:
            return build('NO_SOURCES', related=('SCOPE_EMPTY',))
        if _finite_number(scope_candidates) and float(scope_candidates) <= 0:
            return build('SCOPE_EMPTY', params={'filter': 'scope'},
                         related=('SCOPE_FILTERED', 'NO_SOURCES'))
        return build('NO_SOURCES', related=('SCOPE_EMPTY',))
    if funnel.stop_reason == 'error' or funnel.set_state == 'error':
        return build('JOB_FAILED', params={'stop_reason': funnel.stop_reason or 'error'})
    if control is not None and control.state == 'device_network' and device_lost >= max(proxy_lost, 1):
        return build('DEVICE_NETWORK_DOWN', params={'attributed': device_lost, 'unattributed': proxy_lost},
                     related=('DEVICE_DNS_FAILURE', 'UNREACHABLE', 'TCP_TIMEOUT'))
    if control is not None and control.state == 'target_outage' and target_lost >= max(proxy_lost, 1):
        return build('TARGET_OUTAGE', params={'attributed': target_lost, 'unattributed': proxy_lost},
                     related=('HTTP_5XX', 'TIMEOUT', 'CONTENT_MISMATCH'))
    budget_lost = funnel.stage_counters('budget').lost
    if budget_lost and budget_lost >= _dominant_stage_loss(funnel, FUNNEL_STAGES)[1]:
        return build('E_LIMIT_BUDGET', params={'kind': 'job', 'used': funnel.terminal.get(TIME_OK, 0),
                                                'limit': budget_lost}, related=('BUDGET_EXHAUSTED',))
    expired = funnel.terminal.get(TIME_EXPIRED, 0) + funnel.terminal.get(TIME_TTL_MISSING, 0)
    ok = funnel.terminal.get(TIME_OK, 0)
    time_unknown = funnel.terminal.get(TIME_UNKNOWN, 0) + funnel.terminal.get(TIME_FUTURE, 0)
    rollback = funnel.terminal.get(CLOCK_ROLLBACK, 0)
    if expired and expired >= max(ok, time_unknown, 1):
        return build('E_TIME_TTL_EXPIRED', related=('TTL_EXPIRED',))
    if time_unknown and time_unknown >= max(ok, expired, 1):
        return build('E_TIME_UNKNOWN', params={'field': 'checked_at'},
                     related=('TIME_UNKNOWN', 'TIME_FUTURE', 'E_TIME_FUTURE'))
    if rollback and rollback >= max(ok, expired, 1):
        return build('E_TIME_CLOCK_ROLLBACK', related=('CLOCK_ROLLBACK',))
    exported = totals.get('exported')
    passed = totals.get('passed')
    if _finite_number(passed) and float(passed) > 0 and (exported is None or float(exported) <= 0):
        local_filtered = totals.get('local_filtered') or 0
        if _finite_number(local_filtered) and float(local_filtered) > 0:
            return build('SCOPE_DENYLIST', params={'reason': 'local denylist', 'stage': 'export'},
                         related=('SCOPE_FILTERED_ALL',))
        return build('SCOPE_FILTERED_ALL', params={'stage': 'export', 'count': int(passed)},
                     related=('SCOPE_FILTERED',))
    scope_lost = funnel.stage_counters('scope').lost
    if _finite_number(exported) and float(exported) <= 0 and _finite_number(scope_lost) and float(scope_lost) > 0:
        return build('SCOPE_FILTERED_ALL', params={'stage': 'scope', 'count': int(scope_lost)},
                     related=('SCOPE_FILTERED',))
    stage, code, count = funnel.dominant()
    if count and funnel.lost_total():
        return build(code if is_known_code(code) else 'NO_PROXIES',
                     params={'stage': stage, 'count': count},
                     related=tuple(dict.fromkeys(item[1] for item in funnel.losses() if item[2])))
    if funnel.terminal.get('unknown_verdict'):
        code = funnel.verdicts.most_common(1)[0][0] if funnel.verdicts else 'SCREEN_ERROR'
        return build(code if is_known_code(code) else 'SCREEN_ERROR', related=('NO_DNSBL_ZONES',))
    return build('NO_PROXIES', params={'time_ok': ok},
                 related=tuple(code for _stage, code, _count in funnel.losses()[:5]))


def source_rows(funnel: Funnel) -> int:
    return sum(report.rows for report in funnel.sources)


def funnel_explanation(funnel: Funnel, *, lang: str | None = None) -> str | None:
    """One rendered paragraph for a user, or None when results exist."""
    zero = explain_zero(funnel, lang=lang)
    return None if zero is None else zero.render(lang)


# --- bounded device and target control ----------------------------------------

@dataclasses.dataclass(frozen=True)
class ControlLimits:
    """Hard bounds for the control check.

    The check is opt-in and capped: `total_requests` counts every outbound
    attempt, `max_targets` limits how many job targets are probed, and a zero
    `total_requests` means "do not touch the network at all".
    """

    total_requests: int = 3
    connect_timeout_s: float = 3.0
    per_check_timeout_s: float = 5.0
    max_targets: int = 1
    max_redirects: int = 2
    max_bytes: int = 64 * 1024
    user_agent: str = 'ProxyWorkbench-diagnostics'

    def validate(self) -> None:
        if self.total_requests < 0:
            raise ValueError(tr('total_requests не может быть отрицательным', 'total_requests must not be negative'))
        if self.connect_timeout_s <= 0 or self.per_check_timeout_s <= 0:
            raise ValueError(tr('таймауты контроля должны быть положительными',
                                'control timeouts must be positive'))
        if self.max_targets < 0:
            raise ValueError(tr('max_targets не может быть отрицательным', 'max_targets must not be negative'))


DEFAULT_CONTROL_LIMITS = ControlLimits()


@dataclasses.dataclass(frozen=True)
class ControlProbe:
    kind: str
    target: str
    label: str


CONTROL_CHECKS: tuple[ControlProbe, ...] = (
    ControlProbe('resolve', CONTROL_RESOLVE_HOST, tr('разрешение имени', 'name resolution')),
    ControlProbe('connect', f'{CONTROL_CONNECT_ADDRESS[0]}:{CONTROL_CONNECT_ADDRESS[1]}',
                 tr('исходящее соединение', 'outbound connection')),
)


@dataclasses.dataclass
class ControlVerdict:
    """Whether the machine and the target are healthy, so proxy blame is fair."""

    state: str
    checked: int
    limit: int
    probes: tuple[dict[str, Any], ...] = ()
    code: str | None = None
    reason: str = ''

    @property
    def network_ok(self) -> bool:
        return self.state in ('ok', 'skipped')

    def blame(self, stage: str) -> str:
        """Whose fault a loss at `stage` is: ours, the target's or the proxy's."""
        if self.state == 'device_network' and stage in ('device_network', 'dns', 'tcp', 'handshake', 'tls'):
            return 'device'
        if self.state == 'target_outage' and stage in ('target', 'assertion', 'rate_limit'):
            return 'target'
        return 'proxy'

    def to_dict(self) -> dict[str, Any]:
        return {'state': self.state, 'checked': self.checked, 'limit': self.limit, 'code': self.code,
                'reason': self.reason, 'probes': [dict(probe) for probe in self.probes]}


def _default_control_check(probe: ControlProbe, limits: ControlLimits) -> dict[str, Any]:
    """One bounded control attempt.  Never sends a body, never follows long chains."""
    started = time.monotonic()
    outcome: dict[str, Any] = {'kind': probe.kind, 'target': probe.target, 'ok': False, 'code': None}
    if probe.kind == 'resolve':
        previous = socket.getdefaulttimeout()
        try:
            socket.setdefaulttimeout(limits.connect_timeout_s)
            socket.getaddrinfo(probe.target, None, proto=socket.IPPROTO_TCP)
            outcome['ok'] = True
        except OSError:
            outcome['code'] = 'DEVICE_DNS_FAILURE'
        finally:
            socket.setdefaulttimeout(previous)
        outcome['ms'] = round((time.monotonic() - started) * 1000, 2)
        return outcome
    host, _, port = probe.target.rpartition(':')
    if probe.kind == 'connect':
        try:
            with socket.create_connection((host, int(port)), limits.connect_timeout_s):
                outcome['ok'] = True
        except OSError:
            outcome['code'] = 'DEVICE_NETWORK_DOWN'
        outcome['ms'] = round((time.monotonic() - started) * 1000, 2)
        return outcome
    import httpx  # imported lazily: diagnostics must not need a network stack to load

    limits_kw = dict(timeout=limits.per_check_timeout_s, follow_redirects=bool(limits.max_redirects))
    try:
        with httpx.Client(trust_env=False, verify=True, limits=httpx.Limits(max_redirects=limits.max_redirects),
                          **limits_kw) as client:
            with client.stream('GET', probe.target, headers={'user-agent': limits.user_agent,
                                                            'accept': '*/*'}) as response:
                outcome['status'] = response.status_code
                if response.status_code == 429:
                    outcome['code'] = 'RATE_LIMITED'
                elif response.status_code >= 500:
                    outcome['code'] = 'TARGET_OUTAGE'
                else:
                    outcome['ok'] = True
                for _ in response.iter_bytes():
                    break
    except Exception as exc:  # the control must not raise into the caller
        _stage, outcome['code'] = classification(exc)
    outcome['ms'] = round((time.monotonic() - started) * 1000, 2)
    return outcome


def check_control(*, targets: Sequence[Mapping[str, Any]] = (), limits: ControlLimits = DEFAULT_CONTROL_LIMITS,
                 checker: Callable[[ControlProbe, ControlLimits], Mapping[str, Any]] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> ControlVerdict:
    """Check the device and the job targets within a hard request budget.

    Returns a verdict instead of raising.  `checker` exists so the behaviour can
    be exercised with a local scenario; the default implementation performs at
    most `limits.total_requests` outbound attempts, and none at all when the
    budget is zero.
    """
    limits.validate()
    run = checker or _default_control_check
    probes: list[ControlProbe] = list(CONTROL_CHECKS)
    for target in targets[:max(limits.max_targets, 0)]:
        url = str(target.get('url') or '').strip()
        if url:
            probes.append(ControlProbe('http', url, tr('ответ цели', 'target response')))
    verdict = ControlVerdict(state='skipped', checked=0, limit=limits.total_requests)
    if limits.total_requests <= 0 or not probes:
        return verdict
    results: list[dict[str, Any]] = []
    for probe in probes:
        if verdict.checked >= limits.total_requests:
            break
        verdict.checked += 1
        try:
            outcome = dict(run(probe, limits) or {})
        except Exception as exc:  # a broken checker is a control failure, not a crash
            _stage, code = classification(exc)
            outcome = {'ok': False, 'code': code}
        outcome.setdefault('kind', probe.kind)
        outcome.setdefault('target', probe.target)
        results.append({key: value for key, value in outcome.items() if key != 'body'})
    verdict.probes = tuple(results)
    device_bad = next((item for item in results if item['kind'] in ('resolve', 'connect') and not item.get('ok')), None)
    if device_bad is not None:
        verdict.state = 'device_network'
        verdict.code = device_bad.get('code') or ('DEVICE_DNS_FAILURE' if device_bad['kind'] == 'resolve'
                                                  else 'DEVICE_NETWORK_DOWN')
        verdict.reason = CONTROL_CONNECT_REASON if device_bad['kind'] == 'connect' else tr(
            'Устройство не разрешает имена, поэтому ни один адрес не может быть проверен.',
            'This device cannot resolve names, so no address can be checked.')
    else:
        target_bad = next((item for item in results if item['kind'] == 'http' and not item.get('ok')), None)
        if target_bad is not None:
            verdict.state = 'target_outage'
            verdict.code = target_bad.get('code') or 'TARGET_OUTAGE'
            verdict.reason = tr('Цель не отвечает так, как ожидает профиль; это не ошибка адресов.',
                                'The target does not answer as the profile expects; this is not an address error.')
        elif all(item.get('ok') for item in results):
            verdict.state = 'ok'
        else:
            verdict.state = 'unknown'
            verdict.code = 'UNKNOWN_FAILURE'
            verdict.reason = tr('Проверка сети дала неопределённый результат.',
                                'The network check gave an indeterminate result.')
    return verdict


# --- redaction ----------------------------------------------------------------

SECRET_KEY_PATTERN = re.compile(
    r'(?:^|[_\-.])(?:pass(?:word|wd)?|pwd|secret|token|api[_\-]?key|auth(?:orization)?|credential|cookie|session|signature)'
    r'(?:$|[_\-.])', re.IGNORECASE)
SECRET_HEADER_NAMES = frozenset({'authorization', 'proxy-authorization', 'cookie', 'set-cookie',
                                 'x-api-key', 'api-key', 'x-auth-token', 'x-csrf-token'})
SECRET_QUERY_KEYS = frozenset({'token', 'access_token', 'api_key', 'apikey', 'key', 'secret', 'password',
                               'passwd', 'pwd', 'auth', 'session', 'signature', 'sig'})
USERINFO_RE = re.compile(r'(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)[^/@\s]+@')
QUERY_SECRET_RE = re.compile(r'(?P<key>[?&;](?:' + '|'.join(sorted(SECRET_QUERY_KEYS)) + r')=)[^&\s;#]+',
                             re.IGNORECASE)
BEARER_RE = re.compile(r'(?P<prefix>\b(?:Bearer|Basic|Token)\s+)\S+', re.IGNORECASE)


@dataclasses.dataclass(frozen=True)
class Redaction:
    """What a diagnostic bundle is allowed to keep."""

    credentials: bool = True
    headers: bool = True
    query: bool = True
    hosts: bool = False
    placeholder: str = '***'

    def __post_init__(self) -> None:
        if not isinstance(self.placeholder, str) or not self.placeholder:
            raise ValueError('placeholder must be a non-empty string')


DEFAULT_REDACTION = Redaction()


@dataclasses.dataclass(frozen=True)
class RedactionNote:
    """A record that something was removed.  Never carries the removed value."""

    path: str
    kind: str

    def to_dict(self) -> dict[str, str]:
        return {'path': self.path, 'kind': self.kind}


def _redact_text(value: str, policy: Redaction) -> tuple[str, tuple[str, ...]]:
    kinds: list[str] = []
    result = value
    if policy.credentials:
        result, count = USERINFO_RE.subn(lambda m: m.group('scheme') + policy.placeholder + '@', result)
        if count:
            kinds.append('credentials')
        result, count = BEARER_RE.subn(lambda m: m.group('prefix') + policy.placeholder, result)
        if count:
            kinds.append('header')
    if policy.query:
        result, count = QUERY_SECRET_RE.subn(lambda m: m.group('key') + policy.placeholder, result)
        if count:
            kinds.append('query')
    if policy.hosts:
        parts = urlsplit(result)
        if parts.hostname and parts.scheme in ('http', 'https'):
            netloc = policy.placeholder if not parts.port else f'{policy.placeholder}:{parts.port}'
            result = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
            kinds.append('host')
    return result, tuple(dict.fromkeys(kinds))


def redact_value(value: Any, policy: Redaction = DEFAULT_REDACTION, *, path: str = '$') -> Any:
    """Return a copy of `value` with credentials, secrets and headers removed."""
    redacted, _notes = redact_with_notes(value, policy, path=path)
    return redacted


def redact_with_notes(value: Any, policy: Redaction = DEFAULT_REDACTION, *,
                      path: str = '$') -> tuple[Any, tuple[RedactionNote, ...]]:
    """Redact `value` and report where something was removed, without the value."""
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        notes: list[RedactionNote] = []
        for key, item in value.items():
            name = str(key)
            child = f'{path}.{name}'
            lowered = name.lower()
            if policy.headers and lowered in SECRET_HEADER_NAMES:
                out[name] = policy.placeholder
                notes.append(RedactionNote(child, 'header'))
            elif policy.credentials and SECRET_KEY_PATTERN.search(lowered):
                out[name] = policy.placeholder
                notes.append(RedactionNote(child, 'secret'))
            else:
                redacted, child_notes = redact_with_notes(item, policy, path=child)
                out[name] = redacted
                notes.extend(child_notes)
        return out, tuple(notes)
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        redacted = [redact_with_notes(item, policy, path=f'{path}[{index}]') for index, item in enumerate(items)]
        notes = [note for _value, item_notes in redacted for note in item_notes]
        return [item for item, _notes in redacted], tuple(notes)
    if isinstance(value, str):
        redacted, kinds = _redact_text(value, policy)
        return redacted, tuple(RedactionNote(path, kind) for kind in kinds)
    if isinstance(value, Path):
        return str(value), ()
    if isinstance(value, (int, float, bool)) or value is None:
        return value, ()
    return str(value), ()


# --- diagnostic bundle --------------------------------------------------------

DEFAULT_BUNDLE_SAMPLE = 25


@dataclasses.dataclass
class DiagnosticBundle:
    """A local, redacted report.  Nothing is written until `save` is called."""

    payload: dict[str, Any]
    redactions: tuple[RedactionNote, ...]
    created_at: float
    sample_size: int = 0
    total_size: int = 0
    truncated: bool = False
    schema_version: int = BUNDLE_SCHEMA_VERSION

    def to_json(self) -> str:
        return json.dumps(self.payload, indent=2, ensure_ascii=False, sort_keys=True) + '\n'

    def preview(self, limit: int = 4000) -> str:
        """What the user sees before anything touches the disk."""
        head = (tr('Диагностический пакет Proxy Workbench.', 'Proxy Workbench diagnostic bundle.')
                + f' schema={self.schema_version}, {tr("создан", "created")}='
                + f'{int(self.created_at)}, {tr("строк в выборке", "sampled rows")}={self.sample_size}'
                + f'/{self.total_size}, {tr("заменено секретов", "redacted items")}={len(self.redactions)}\n')
        body = self.to_json()
        if len(body) > limit:
            body = body[:limit] + '\n... ' + tr('обрезано для предпросмотра', 'truncated for preview')
        return head + body

    def describe(self, lang: str | None = None) -> str:
        return _pick('Пакет содержит версии, область проверки, состояние задания, счётчики воронки '
                     'и выборку строк. Пароли, токены и заголовки авторизации заменены на «***». '
                     'Пакет сохраняется только локально и никуда не отправляется.',
                     'The bundle holds versions, the check scope, job state, funnel counters and a row '
                     'sample. Passwords, tokens and authorization headers are replaced with "***". '
                     'It is stored locally and sent nowhere.', lang)

    def save(self, path: str | os.PathLike[str], *, overwrite: bool = False) -> Path:
        """Write the bundle to `path`.  Refuses to clobber unless asked."""
        target = Path(path)
        if target.exists() and not overwrite:
            raise FileExistsError(f'refusing to overwrite an existing file: {target}')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_json(), encoding='utf-8')
        return target


def build_bundle(*, environment: Mapping[str, Any] | None = None, status: Mapping[str, Any] | None = None,
                 rows: Iterable[Mapping[str, Any]] = (), sources: Iterable[Mapping[str, Any] | SourceReport] = (),
                 funnel: Funnel | None = None, job: Mapping[str, Any] | None = None,
                 control: ControlVerdict | None = None, zero: ZeroResult | None = None,
                 sample: int = DEFAULT_BUNDLE_SAMPLE, policy: Redaction = DEFAULT_REDACTION,
                 now: float | None = None, version: int = BUNDLE_SCHEMA_VERSION) -> DiagnosticBundle:
    """Assemble the local diagnostic bundle from already available data.

    The caller passes the pieces; the module adds the environment it can read
    locally, redacts everything, and never opens a socket or writes a file.
    """
    created_at = time.time() if now is None else float(now)
    row_list = [row for row in rows if isinstance(row, Mapping)]
    if funnel is None:
        # Materialised above, so a generator of rows is not consumed twice.
        funnel = build_funnel(row_list, status=status, sources=sources, control=control)
    sampled = row_list[:max(sample, 0)]
    payload: dict[str, Any] = {
        'schema_version': version,
        'product': 'Proxy Workbench',
        'created_at': created_at,
        'environment': dict(environment) if environment is not None else local_environment(),
        'policy': {'max_age_seconds': LEGACY_MAX_AGE_SECONDS, 'clock_skew_seconds': CLOCK_SKEW_SECONDS},
    }
    if status is not None:
        payload['status'] = dict(status)
    if job is not None:
        payload['job'] = dict(job)
    if sources:
        payload['sources'] = [dataclasses.asdict(report) if isinstance(report, SourceReport) else dict(report)
                              for report in sources]
    payload['funnel'] = funnel.to_dict()
    if control is not None:
        payload['control'] = control.to_dict()
    if zero is not None:
        payload['zero_result'] = zero.to_dict()
    payload['rows'] = {'total': len(row_list), 'sampled': len(sampled), 'items': sampled}
    redacted, notes = redact_with_notes(payload, policy)
    return DiagnosticBundle(payload=redacted, redactions=notes, created_at=created_at,
                            sample_size=len(sampled), total_size=len(row_list),
                            truncated=len(sampled) < len(row_list))


def local_environment() -> dict[str, Any]:
    """Versions and runtime facts a support report needs, read from the process."""
    from . import branding  # local import keeps this module usable without branding

    return {
        'product_version': branding.PRODUCT_VERSION,
        'python': sys.version.split()[0],
        'platform': platform.platform(),
        'machine': platform.machine(),
        'os': platform.system(),
        'frozen': bool(getattr(sys, 'frozen', False)),
    }


# --- health -------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class HealthCheck:
    name: str
    ok: bool
    code: str | None = None
    detail: str = ''
    action: str = ''
    params: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {'name': self.name, 'ok': self.ok, 'code': self.code, 'detail': self.detail,
                'action': self.action, 'params': dict(self.params)}


@dataclasses.dataclass
class HealthReport:
    checks: tuple[HealthCheck, ...] = ()

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    @property
    def failures(self) -> tuple[HealthCheck, ...]:
        return tuple(check for check in self.checks if not check.ok)

    def check(self, name: str) -> HealthCheck | None:
        return next((item for item in self.checks if item.name == name), None)

    def to_dict(self) -> dict[str, Any]:
        return {'ok': self.ok, 'checks': [check.to_dict() for check in self.checks]}

    def render(self, lang: str | None = None) -> str:
        lines = []
        for check in self.checks:
            mark = 'ok' if check.ok else (check.code or 'failed')
            line = f'[{mark}] {check.name}: {check.detail}'
            if not check.ok and check.action:
                prefix = 'Действие' if _pick('ru', 'en', lang) == 'ru' else 'Action'
                line += f'\n{prefix}: {check.action}'
            lines.append(line)
        return '\n'.join(lines)


def health_report(*, version: str | None = None, schema_version: Any = None, scope: Mapping[str, Any] | None = None,
                  job: Mapping[str, Any] | None = None, profile: Mapping[str, Any] | None = None,
                  control: ControlVerdict | None = None, limits: ControlLimits | None = None,
                  max_age_seconds: Any = None) -> HealthReport:
    """Version, scope and job health with a code and an action per problem."""
    checks: list[HealthCheck] = []

    def fail(name: str, code: str, detail: str, **params: Any) -> None:
        help_text = help_for(code, params=params)
        checks.append(HealthCheck(name, False, code, detail, help_text.action, params))

    if version is None:
        fail('version', 'VERSION_UNKNOWN', tr('Версия программы не передана', 'Program version was not provided'))
    else:
        checks.append(HealthCheck('version', True, None, f'{version}'))

    if schema_version is None:
        fail('schema', 'SCHEMA_UNKNOWN', tr('Версия схемы базы неизвестна', 'Database schema version is unknown'))
    else:
        checks.append(HealthCheck('schema', True, None, f'user_version={schema_version}'))

    if scope is None:
        fail('scope', 'SCOPE_UNSPECIFIED', tr('Область проверки не задана', 'The check scope is not set'),
             field='scope')
    else:
        missing = [name for name in ('collection_id', 'profile_id', 'profile_revision') if scope.get(name) in (None, '')]
        if missing:
            fail('scope', 'SCOPE_UNSPECIFIED', tr(f'Не зафиксировано: {", ".join(missing)}',
                                                  f'Not pinned: {", ".join(missing)}'), field=missing[0])
        else:
            checks.append(HealthCheck('scope', True, None,
                                      f'collection={scope["collection_id"]} profile={scope["profile_id"]}'
                                      f'@{scope["profile_revision"]}'))

    if job is not None:
        state = str(job.get('state') or '')
        if state and state not in ('created', 'queued', 'running', 'paused', 'succeeded', 'partial',
                                   'failed', 'cancelled', 'timed_out'):
            fail('job', 'JOB_FAILED', tr(f'Неизвестное состояние задания: {state}',
                                         f'Unknown job state: {state}'), stop_reason=state)
        else:
            checks.append(HealthCheck('job', True, None, state or tr('нет задания', 'no job')))
    else:
        checks.append(HealthCheck('job', True, None, tr('нет задания', 'no job')))

    if profile is not None and max_age_seconds in (None, 0):
        fail('freshness', 'TTL_EXPIRED', tr('max-age не задан в ревизии профиля',
                                            'max-age is not set in the profile revision'), age_s=0)
    elif max_age_seconds is not None and _finite_number(max_age_seconds) and float(max_age_seconds) <= 0:
        fail('freshness', 'TTL_EXPIRED', tr('max-age должен быть положительным', 'max-age must be positive'),
             age_s=max_age_seconds)
    else:
        checks.append(HealthCheck('freshness', True, None, f'max_age_seconds={max_age_seconds}'
                                  if max_age_seconds is not None else tr('по умолчанию', 'default')))

    if limits is not None:
        try:
            limits.validate()
        except ValueError as exc:
            fail('limits', 'E_LIMIT_CONCURRENCY', str(exc), limit=limits.max_targets)
        else:
            checks.append(HealthCheck('limits', True, None, f'control_requests={limits.total_requests}'))

    if control is not None and control.state in ('device_network', 'target_outage'):
        code = 'DEVICE_NETWORK_DOWN' if control.state == 'device_network' else 'TARGET_OUTAGE'
        fail('control', code, control.reason or code, attributed=control.checked)
    elif control is not None:
        checks.append(HealthCheck('control', True, None, control.state))
    return HealthReport(tuple(checks))


# --- reproducible fixture recipe ----------------------------------------------

@dataclasses.dataclass
class FixtureRecipe:
    """Everything needed to repeat one measurement, so a fixture is reproducible."""

    complete: bool
    targets: tuple[dict[str, Any], ...] = ()
    attempts: int = 1
    timeout_s: float | None = None
    connect_timeout_s: float | None = None
    whole_probe_timeout_s: float | None = None
    request_profile: str = 'workbench'
    profile: str | None = None
    samples: tuple[dict[str, Any], ...] = ()
    code: str | None = None
    source: dict[str, Any] = dataclasses.field(default_factory=dict)
    policy: Redaction = DEFAULT_REDACTION

    def config_json(self) -> str:
        """The profile configuration to re-run this measurement."""
        return json.dumps(self.to_dict()['config'], indent=2, ensure_ascii=False, sort_keys=True) + '\n'

    def to_dict(self) -> dict[str, Any]:
        config: dict[str, Any] = {
            'targets': [dict(target) for target in self.targets],
            'attempts': self.attempts,
            'request_profile': self.request_profile,
        }
        if self.timeout_s is not None:
            config['timeout'] = self.timeout_s
        if self.connect_timeout_s is not None:
            config['connect_timeout'] = self.connect_timeout_s
        if self.whole_probe_timeout_s is not None:
            config['whole_probe_timeout'] = self.whole_probe_timeout_s
        return {'complete': self.complete, 'code': self.code, 'config': config,
                'source': dict(self.source), 'samples': [dict(sample) for sample in self.samples]}

    def render(self, lang: str | None = None) -> str:
        label = _pick('Рецепта воспроизведения замера', 'Measurement reproduction recipe', lang)
        lines = [f'{label} — {_pick("полная" if self.complete else "неполная", "complete" if self.complete else "incomplete", lang)}']
        if not self.complete and self.code:
            help_text = help_for(self.code, lang=lang)
            lines.append(f'{help_text.title} [{self.code}]')
            lines.append(f'{_pick("Действие", "Action", lang)}: {help_text.action}')
        for target in self.targets:
            lines.append(f'- {target.get("method", "GET")} {target.get("url", "")} '
                         f'statuses={target.get("statuses")} contains={target.get("contains")!r} '
                         f'sha256={target.get("sha256")!r}')
        lines.append(f'- attempts={self.attempts} timeout={self.timeout_s} '
                     f'connect_timeout={self.connect_timeout_s} profile={self.request_profile}')
        if self.samples:
            lines.append(f'- {len(self.samples)} ' + _pick('образцов замера', 'measurement samples', lang))
        return '\n'.join(lines)


def fixture_recipe(row: Mapping[str, Any], config: Mapping[str, Any] | None = None, *,
                   policy: Redaction = DEFAULT_REDACTION) -> FixtureRecipe:
    """Build a reproduction recipe for one measured row.

    Without the profile configuration the target URLs and assertions cannot be
    known, and the recipe says so instead of guessing.  Credentials in target
    URLs are redacted, so a recipe is safe to paste into an issue.
    """
    samples = tuple({'target': sample.get('target'), 'attempt': sample.get('attempt'),
                     'status': sample.get('status'), 'error': sample.get('error'), 'ok': bool(sample.get('ok')),
                     'ms': sample.get('ms')}
                    for sample in (row.get('samples') or ()) if isinstance(sample, Mapping))
    source = {'proxy': row.get('proxy'), 'checked_at': row.get('checked_at'), 'samples': len(samples)}
    if not samples and not row.get('error'):
        return FixtureRecipe(False, code='RECIPE_NO_SAMPLES', source=source, policy=policy)
    if not isinstance(config, Mapping) or not config.get('targets'):
        return FixtureRecipe(False, targets=(), attempts=1, samples=samples, code='RECIPE_NO_PROFILE',
                             source=source, policy=policy)
    targets = []
    for target in config.get('targets') or ():
        if not isinstance(target, Mapping):
            continue
        targets.append(redact_value({
            'url': target.get('url', ''),
            'method': target.get('method', 'GET'),
            'statuses': list(target.get('statuses') or ()),
            'contains': target.get('contains'),
            'sha256': target.get('sha256'),
            'headers': dict(target.get('headers') or {}),
        }, policy))
    timeout = config.get('timeout') if _finite_number(config.get('timeout')) else None
    connect = config.get('connect_timeout') if _finite_number(config.get('connect_timeout')) else None
    whole = config.get('whole_probe_timeout') if _finite_number(config.get('whole_probe_timeout')) else None
    return FixtureRecipe(
        complete=True,
        targets=tuple(targets),
        attempts=int(config.get('attempts') or 1),
        timeout_s=float(timeout) if timeout is not None else None,
        connect_timeout_s=float(connect) if connect is not None else None,
        whole_probe_timeout_s=float(whole) if whole is not None else None,
        request_profile=str(config.get('request_profile') or 'workbench'),
        profile=str(row.get('profile')) if row.get('profile') else None,
        samples=samples,
        source=source,
        policy=policy,
    )
