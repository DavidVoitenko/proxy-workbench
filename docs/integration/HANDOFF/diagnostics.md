# Handoff: diagnostics

**Требования:** F10 (диагностика и воронка), F25 (помощь и диагностика). Смежно: §5.4 CONTRACTS (error codes, перевод отдельно от кода), §4.3 (`state_detail`), §2.4 (состояния времени), §6.3 (`blocked` до измерения), дефект 3 и дефект 25.
**Контракт:** `docs/integration/CONTRACTS.ru.md` версия 1.
**База:** ветка `integration/ultra-2026-09-25`, HEAD `d0c986e` (`core.py` уже лежит в ветке), плюс незакоммиченные правки других исполнителей — в мой коммит они не входят.

Владею только `proxy_workbench/diagnostics.py` и `tests/test_diagnostics_*.py`. Всё ниже — просьба, а не сделано мной.

## 1. Прошу внести в чужие файлы

### 1.1 `proxy_workbench/proxytool.py` — коды измерения вместо `type(exc).__name__`

Точки, где сегодня класс исключения кладётся в результат (CONTRACTS §5.4 фиксирует, что `ConnectError`, `ConnectTimeout`, `ReadTimeout`, `ProxyError`, `SSLError` сливаются в одно значение):

- `request_once` (`proxytool.py:1041-1043`),
- `measure_speed` (`proxytool.py:1188-1189`),
- `judge_proxy` (`proxytool.py:1225-1226`),
- `worker` в `scan` (`proxytool.py:1447-1450`),
- `unreachable_result` (`proxytool.py:1271-1274`) — `error='UNREACHABLE'` без стадии.

Что заменить: вызвать `diagnostics.classification(exc)` и записать оба значения.

```python
from . import diagnostics

# request_once, вместо result['error'] = type(exc).__name__
stage, code = diagnostics.classification(exc)
result['error'], result['stage'] = code, stage
```

`classification` уже различает DNS и TCP по тексту сообщения (`DNS_MARKERS`), так что резолвер-ошибка, пришедшая как `ConnectError`, станет `DNS_ERROR` на стадии `dns`, а не `UNREACHABLE` на `tcp`. Новое поле `sample['stage']` — аддитивное; `sample` хранится в JSON payload, потребители, читающие только `ok/ms/status/error/bytes`, не ломаются.

### 1.2 `proxy_workbench/proxytool.py` — `state_detail` и счётчики воронки в `status.json`

`export()`, блок `report = dict(...)` (`proxytool.py:1868-1897`). Контракт §4.3 уже требует `state_detail` со значениями `empty_no_match / all_expired / all_failed / budget_exhausted / stopped / cancelled / crashed`; мой модуль даёт это значение плюс код и действие.

```python
funnel = diagnostics.build_funnel(rows_payloads, status=report, sources=source_reports)
zero = diagnostics.explain_zero(funnel)
if zero is not None:
    report['state_detail'] = zero.code
    report['zero_result'] = zero.to_dict()
report['funnel'] = funnel.to_dict()
```

Аргументы, которые уже есть на руках: `rows_payloads` — это распарсенные payload'ы из цикла выборки (`proxytool.py:1729-1778`), `source_reports` — `reports` из `collect()` (`proxytool.py:889`, словари `dict(source=..., rows=..., invalid=..., blocked=..., complete=..., error=..., format=...)`), `report` — сам status. Ничего нового доставать не нужно и второй проход по БД не требуется.

Почему именно здесь: `state` и `stop_reason` сегодня не различают «ничего не подошло» и «всё истекло» (`proxytool.py:1868-1872`), и без разбора счётчиков пользователь не отличит отказ источника от пустого фильтра.

### 1.3 `proxy_workbench/proxytool.py` — прогресс и события

`publish()` в `scan` (`proxytool.py:1457-1485`) сейчас отдаёт агрегат (`checked`, `passed`, `unreachable`). Прошу добавить в тот же `on_progress` вызов `diagnostics.explain_zero(funnel)` (если строится funnel) и поле `zero` с кодом и действием — это закрывает дефект 25 на стороне источника данных: у потребителя событий появляется машинный код вместо разбора строки лога.

### 1.4 `proxy_workbench/i18n.py` и `proxy_workbench/ui/*` — перевод и help (владелец: поверхность `web`)

- Коды и тексты переводятся существующим `i18n.tr(ru, en)` — свой словарь не нужен и не нужен. Мой модуль импортирует `tr` из `.i18n` и ничего не добавляет в него.
- In-app help по кодам: `diagnostics.help_entries()` возвращает `ErrorHelp(code, stage, domain, known, title, action)` по всем кодам в порядке воронки; `diagnostics.help_for(code, lang=...)` — точечныйlookup. Нужен экран/панель «почему 0 результатов» и возможность открыть помощь по коду из строки.
- Кнопка «Сохранить диагностику»: `bundle = diagnostics.build_bundle(rows=..., status=..., job=..., control=..., zero=...)`, показать `bundle.preview()` и `bundle.describe()`, запись — только по явному действию `bundle.save(path)`. Ничего не отправляется: в модуле нет сетевого кода (проверено тестом, см. §4).
- `ZeroResult.render(lang)` даёт готовый текст «причина + счётчики + действие» — выводить его можно как есть.

`ui/app.js`, `ui/index.html`, `ui/style.css` я не трогаю: это зона поверхности `web` (HANDOFF §1.3/§1.4).

### 1.5 `docs/integration/CONTRACTS.ru.md` §5.4 — дополнить таблицу кодов

Новые **bare** коды (коды строки результата, стиль уже принят в таблице для `UNREACHABLE`/`CONTENT_MISMATCH`):

`TCP_TIMEOUT`, `TCP_REFUSED`, `TCP_RESET`, `TIMEOUT`, `HANDSHAKE_ERROR`, `TLS_ERROR`, `TLS_CERT_INVALID`, `HTTP_3XX/4XX/5XX` (конкретные коды), `CONTENT_TRUNCATED`, `INVALID_PROXY`, `SOURCE_TOO_LARGE`, `SOURCE_LINE_TOO_LARGE`, `SOURCE_CANDIDATE_LIMIT`, `SOURCE_REDIRECT_*`, `SOURCE_INVALID_UTF8`, `SOURCE_PRIVATE_DESTINATION`, `SOURCE_EMPTY`, `SOURCE_UNAVAILABLE`, `TTL_EXPIRED`, `TIME_UNKNOWN`, `TIME_FUTURE`, `CLOCK_ROLLBACK`, `JOB_FAILED`, `VERSION_UNKNOWN`, `SCHEMA_UNKNOWN`, `SCOPE_UNSPECIFIED`, `RECIPE_NO_PROFILE`, `RECIPE_NO_SAMPLES`, `NO_PROXIES`, `NO_SOURCES`, `SCOPE_EMPTY`, `SCOPE_FILTERED`, `SCOPE_FILTERED_ALL`, `SCOPE_DENYLIST`, `SCOPE_REPUTATION_LISTED`, `AUTH_FAILED`, `RATE_LIMITED`, `VAULT_LOCKED`, `BUDGET_EXHAUSTED`, `DEVICE_NETWORK_DOWN`, `DEVICE_DNS_FAILURE`, `TARGET_OUTAGE`.

Параметры кодов объявлены отдельно от текста в `CODES[code].params` (например, `retry_after_s`, `timeout_s`, `max_bytes`, `checked_at`) — это и есть требование «коды и параметры отдельно от перевода».

Новых доменов `E_<DOMAIN>` я **не заводил**: все `E_*`-коды, которые я использую, взяты из §5.4 (`E_LIMIT_BUDGET`, `E_LIMIT_CONCURRENCY`, `E_TIME_*`, `E_STATE_SNAPSHOT_STALE`, `E_STATE_NO_SNAPSHOT`, `E_STATE_POOL_EMPTY`, `E_IMPORT_FORMAT`, `E_SECRET_VAULT_LOCKED`, `E_SECRET_NOT_PROVIDED`, `E_AUTH_RATE_LIMITED`). Это проверено тестом `test_no_error_code_outside_the_contract_canon`, так что таблица §5.4 не расходится с кодом и бамп версии контракта не требуется — нужна только дописка таблицы списком bare-кодов.

Отдельно: `E_TIME_TTL_MISSING` уже используется в `core.py` (`TIME_REASONS`), но в строке TIME таблицы §5.4 его нет. Прошу дописать его в ту же строку — иначе в репозитории есть код без записи в каноне.

Прошу также внести в §5.4 пометку, что `E_SECRET_UPSTREAM_AUTH_FAILED` (§5.4, домен SECRET) и коды источников не дублируются, а читаются из `diagnostics.CODES`.

## 2. Что уже сделано у меня

Публичный API `proxy_workbench/diagnostics.py` (без DDL, без сети при импорте, без записи на диск без явного `save`):

| Что | Вызов | Что возвращает |
| --- | --- | --- |
| Справочник кодов | `CODES`, `CONTRACT_CODES`, `CODE_STAGES`, `codes_for_stage(stage)`, `help_entries()` | `dict[str, Code]` / кортеж `ErrorHelp` |
| Help по коду | `help_for(code, params=..., lang='ru'/'en')` | `ErrorHelp(code, stage, domain, known, title, action, params, values)` |
| Классификация | `classification(value)`, `stage_of(value)`, `explain_error(exc, lang=...)` | `(stage, code)` / `ErrorHelp`; неизвестное значение → `UNKNOWN_FAILURE`, не тишина |
| Ошибка с действием | `ActionableError(code, params=..., cause=...)`, `actionable(...)` | исключение с `.code`, `.stage`, `.action()`, `.render()` |
| Воронка | `build_funnel(rows, status=..., sources=..., control=..., admitted=..., now=..., last_seen=..., backfill_legacy_ttl=..., future_tolerance_s=..., legacy_max_age_s=...)` | `Funnel` |
| Ответ «почему 0» | `explain_zero(funnel, lang=...)`, `funnel_explanation(funnel, lang=...)` | `ZeroResult(code, stage, summary, action, counters, params, related)` |
| Контроль сети/цели | `check_control(targets=..., limits=ControlLimits(...), checker=...)` | `ControlVerdict(state, checked, limit, probes, code, reason)`; `blame(stage)` → `device`/`target`/`proxy` |
| Redaction | `redact_value(value, Redaction(...))`, `redact_with_notes(...)` | значение и `RedactionNote(path, kind)` без значения |
| Пакет | `build_bundle(...)`, `bundle.preview()`, `bundle.save(path, overwrite=False)`, `bundle.describe()` | `DiagnosticBundle` |
| Здоровье | `health_report(version=..., schema_version=..., scope=..., job=..., profile=..., control=..., max_age_seconds=...)` | `HealthReport.checks: tuple[HealthCheck, ...]` |
| Рецепта фикстуры | `fixture_recipe(row, config=None)` | `FixtureRecipe` c `config_json()` и `render()` |

Ключевые контракты, на которые можно опереться:

- **Стадии воронки** (`FUNNEL_STAGES`, 15 штук, порядок фиксирован): `source, download, parser, scope, device_network, dns, tcp, handshake, tls, target, assertion, auth, rate_limit, budget, freshness`. Ровно те, что перечислены в F10. Счётчики ведутся в двух цепочках с разными единицами: `source/download/parser` считают источники и строки, остальные — прокси. Смешивать их в одну сумму нельзя, поэтому `Funnel.entry()` — это прокси, `Funnel.source_entry()` — источники.
- **Состояния времени названы так же, как в `core.py`.** `diagnostics.TIME_STATES` = `{time_ok, time_unknown, time_future, clock_rollback, time_expired, time_ttl_missing}` — те же строки, что `core.TIME_STATES`, и `LEGACY_MAX_AGE_SECONDS` = `core.LEGACY_TTL_SECONDS` (7200). `core` остаётся единственным органом допуска, `data_state` — это взгляд воронки на ту же ось; расхождение проверяется тестом на шести случаях (свежая, истёкшая, будущая, без времени, legacy-без-срока, откат часов) против `core.time_state_of`. `build_funnel(admitted=...)` ждёт **callable от строки** — то есть `core.admit`, связанный своими аргументами:
  `admitted=lambda row: core.admit(row, scope, access, policy, now).admitted`
  (`core.admit` требует scope/access/policy/now позиционно, поэтому голая передача функции не подходит).
- **`build_funnel` ничего не выдумывает.** Потеря ставится на той стадии, где она реально произошла (по `error`/`samples`/`reputation` строки), а если потерь нет — строка попадает в терминальные состояния `measured / stale / time_unknown / time_future / clock_rollback / unmeasured / unknown_verdict`. Отдельный вызов `Funnel.drop(stage, code, count, attribution)` есть для конвейера, который знает стадию сам.
- **Атрибуция.** `check_control` даёт `ControlVerdict`; `build_funnel(..., control=...)` помечает потерю как `device`/`target`/`proxy`, и `Funnel.attribution` это показывает. Глобальная недоступность сети или цели **не** становится репутацией адресов: `ZeroResult.params` содержит `attributed` и `unattributed`, а код — `DEVICE_NETWORK_DOWN` или `TARGET_OUTAGE`, а не `UNREACHABLE`.
- **Контроль ограничен и опционален.** `ControlLimits.total_requests` — жёсткий предел числа исходящих попыток (по умолчанию 3), `max_targets` — сколько целей задания проверяется, `total_requests=0` → `state='skipped'` и ни одного вызова. Реализация по умолчанию берёт цели **из задания** (аргумент `targets`), а не из фиксированного внешнего адреса; `checker` подменяем, поэтому поведение проверяется локально.
- **Bundle ничего не пишет сам.** `build_bundle` только собирает и редактирует; `save()` — единственная запись, и она отказывается затирать существующий файл без `overwrite=True`.
- **Перевод.** Тексты — через `i18n.tr`; коды, параметры и стадии от перевода не зависят. Для `lang='ru'/'en'` есть явный выбор, независимый от терминала.

## 3. Совместимость

- **Что ломается, если не внести:** §1.1 — без него F10 «разделять стадии» остаётся невыполнимым, потому что движок физически не пишет стадию; §1.2 — без него `state_detail` в `status.json` не появится, и дефект 3 («один истёкший убивает пул») останется с двумя значениями на один смысл.
- **Что НЕ ломается:** ничего из моего модуля не меняет существующие файлы и не требует их изменения для работы. `diagnostics` не импортирует `proxytool`, `db`, `api` или `gui`; наоборот, они могут импортировать его. До интеграции `proxytool` работает как раньше.
- Схемы БД не касаюсь: `db.py` на момент написания ещё не существует, DDL не пишу (HANDOFF §2.2), таблиц для диагностики не прошу.
- Риск, который стоит назвать: `Funnel` считает по строкам результата (`results.payload`), а не по таблице `observations` из миграции 4. Пока наблюдения не наполняются движком, это единственный доступный источник; у observation-записей те же поля (`samples`/`error`/`checked_at`/`valid_until`), поэтому переход на них — это подмена источника строк, а не правка кода. Прошу интегратора выбрать источник при подключении §1.1–1.3 и не передавать в `build_funnel` обе таблицы сразу.

## 4. Проверки

Выполнены в этой сессии, из корня репозитория:

```
$ .venv/bin/python -m unittest tests.test_diagnostics_codes
Ran 24 tests in 0.001s
OK

$ .venv/bin/python -m unittest tests.test_diagnostics_funnel
Ran 36 tests in 0.004s
OK

$ .venv/bin/python -m unittest tests.test_diagnostics_bundle
Ran 29 tests in 0.068s
OK
```

Одна команда на все три модуля:

```
$ .venv/bin/python -m unittest tests.test_diagnostics_codes tests.test_diagnostics_funnel tests.test_diagnostics_bundle
Ran 89 tests in 0.071s
OK
```

Что именно доказывают тесты, а не «структуру кода»:

- сценарий приёмки F10 (`test_a_total_upstream_loss_names_the_stage_and_the_action` + `test_the_same_run_with_results_has_nothing_to_explain`): 8 строк с `UNREACHABLE` и `exported=0` дают код `UNREACHABLE`, стадию `tcp`, счётчик `count=8`, текст с `Действие:` и без слова `Traceback`; после «починки» (появились результаты) объяснение исчезает;
- DNS против TCP (`test_a_connect_error_is_tcp_but_a_resolver_failure_is_dns`) на настоящих `httpx.ConnectError`;
- согласие с admission-контрактом (`test_the_funnel_and_the_admission_contract_agree_on_the_time_state`): состояние времени у `diagnostics.data_state` совпадает с `core.time_state_of` на шести случаях, включая legacy-строку без срока и откат часов; при отсутствии `core.py` тест пропускается, а не падает;
- скачивание против парсера (`test_download_and_parser_failures_are_separated`) на отчётах `collect()`;
- глобальная недоступность (`test_a_global_outage_does_not_taint_every_address`, `test_a_target_outage_does_not_taint_every_address`): `attribution == {'device': 5}` / `{'target': 3}`, `proxy == 0`;
- ограниченность контроля (`test_the_number_of_outbound_attempts_is_capped`, `test_a_zero_budget_means_the_network_is_never_touched`);
- canary-секрет `canary-PW-4f2b91c7d0e3` отсутствует в `payload`, в `preview()` и в байтах записанного файла (`test_the_canary_is_absent_from_payload_preview_and_file`); в заметках редактирования значения нет (`test_notes_never_carry_the_value_they_removed`);
- «никакой скрытой отправки»: `test_importing_the_module_opens_no_socket` запускает **отдельный процесс** с подменёнными `socket.socket`, `socket.create_connection`, `socket.getaddrinfo` на функции, бросающие `AssertionError`, и в нём импортируется модуль, строится пакет и он сохраняется на диск; `test_building_and_saving_uses_no_socket` делает то же в процессе теста.

Что осталось непрочитанным/непроверенным:

- полный `unittest discover -s tests` не запускался — по условию его выполняет интегратор после сборки всех модулей;
- реальный сетевой путь `check_control` (default `_default_control_check`) не выполнялся: по условию задачи внешние проверки запрещены, поэтому проверены только ветки с подставным `checker` и ветка `skipped`;
- `db.py` на момент написания существует (`tests/test_db_*.py` в дереве), но диагностика его не касается: DDL и таблиц не требуется, проверки с мигратором не писались;
- чужие модули (`apikeys`, `probes`, `secrets`, `pools`, `importer`, `exportsvc`, `sourcedesk`, `servicecatalog`, `geo`, `profiles`) в дереве есть, но их тесты я не запускал и их API не использовал — единственный прочитанный чужой модуль `core.py` (read-only, для проверки согласованности имён состояний).

## 5. Открытые вопросы

1. `ZeroResult.code` для причин уровня «окружение» возвращается bare-кодом (`DEVICE_NETWORK_DOWN`, `TARGET_OUTAGE`, `UNREACHABLE`), а для причин уровня «состояние продукта» — `E_*` (§5.4). Так смешение выглядит осмысленным (в §5.4 таблица уже содержит оба вида), но если владелец контракта предпочитает, чтобы `zero_result.code` был всегда `E_*`, нужен один новый код в домене `CONTROL` — прошу решение до интеграции.
2. Допуск по умолчанию для будущего времени различается: `core.Policy.future_tolerance_seconds = 0.0`, мой `CLOCK_SKEW_SECONDS = 60.0`. Семантика одна и та же (`checked_at > now + tolerance` → `time_future`, §2.4), различается только значение по умолчанию, а оно по контракту приходит из ревизии профиля. Прошу при интеграции передавать `Policy.future_tolerance_seconds` в `build_funnel(future_tolerance_s=...)`, иначе воронка и допуск будут считать «будущим» разные множества строк.
3. `build_funnel` принимает `admitted` (результат `core.admit`). Пока он не передан, `totals['passed']` не заполняется, и нулевой результат объясняется по строкам и `status`. Сквозную точность проверит интегратор.
4. `LEGACY_MAX_AGE_SECONDS = 7200` совпадает с `core.LEGACY_TTL_SECONDS` (значение проверено тестом), но это два независимых числа в двух модулях. Прошу решить, какое из них authoritative, чтобы синхронное изменение не разошлось.
