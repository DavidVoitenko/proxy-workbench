# Handoff: интеграция — что подключено, что осталось за чужими файлами

**Требования:** F01–F29 (подключение), дефекты 1, 2, 3, 4, 6, 7, 8, 9, 10, 12, 13, 14, 15, 20
**База:** ветка `integration/ultra-2026-09-25`, HEAD `d917d91`
**Мои файлы:** `proxytool.py`, `api.py`, `apiv1.py`, `core.py`, `importer.py`,
`scheduler.py`, `desktop.py`, `paths.py` (не менялся — не потребовался).

Чужие модули (`probes`, `db`, `secrets`, `apikeys`, `jobs`, `pools`, `sourcedesk`,
`source_catalog`, `source_adapters`, `source_management`, `exportsvc`, `geo`,
`diagnostics`, `importer`'s владелец тестов) **не переписывались** — вызываются.

---

## 1. Пробы: transport-seam (F01, F07, F20, дефекты 13–15)

`proxytool.Transport` — четыре метода, которые `probes` документирует и которых
не было ни в одном файле дерева:

| метод | что делает |
|---|---|
| `send(request, *, options)` | один запрос, `max_bytes` как **настоящий** потолок (чанк ограничен `min(8192, max_bytes)`, а не «проверить после 64 КБ») |
| `download(target, *, options, reuse)` | `TransferTrace` с посегментным таймингом |
| `websocket(request, *, options, spec)` | настоящий апгрейд RFC 6455 + ping/pong (`asyncio.open_connection` не умеет прокси, CONNECT написан руками) |
| `hold(request, *, options, spec)` | соединение, которое должно продержаться |

`send`/`download` идут через `httpx` (тот же клиент, тот же SOCKS4-транспорт,
тот же TLS-контекст, что и весь продукт).

Подключено:

* `check_proxy` берёт режим из `probes.resolve_mode`, а **уровень доказательства**
  — из `probes.evidence_for(PlanOutcome)`, который собирает `plan_outcome_of()` из
  уже сделанных замеров. Никаких дополнительных сетевых запросов. Строка получает
  `evidence`, `is_working`, `code`, `stage`, `probe_targets` (публичные
  `TargetOutcome.to_public()`).
* `measure_speed` больше не считает окно руками: байты отдаёт транспорт, число и
  состояние — `probes.run_speed_test` → `probes.measure_speed`. Один чанк даёт
  `state='insufficient'` **без числа**.
* `judge_proxy` использует `anonymity.classify_detail` вместо `classify`: код
  (`JUDGE_INVALID` / `JUDGE_CHALLENGE` / `JUDGE_UNVERIFIED`) и `exit_ip` едут в строку.
* `detect_own_ips` вызывает `extract_public_ips(..., global_only=False)`.
* `export(min_anonymity=...)` без judge теперь даёт
  `E_VALIDATION_ANONYMITY_REQUIRED` (`probes.require_anonymity`) вместо тихого
  понижения до `any`.

**Живой прогон** (`tests/test_integration_transport.py`, реальный CONNECT-туннель
на 127.0.0.1):

```
send       -> status=200 bytes=1024 code=None ttfb=10.14ms
download   -> state=ok mbps=9.76 bytes=2097152 chunks=78 conn=cold
websocket  -> state=ok code=None pongs=2 upgrade=websocket
hold       -> state=ok code=None sustained=1.003s chunks=10
```

**Просит владельца `probes.py`:** `tests/test_probearea_modes.py` уже доказывает
лестницу; `verified_at` у `BASIC_PROBES` всё ещё `None` — нужен ручной прогон
внешних ответов, условия задачи внешнюю сеть запрещают.

---

## 2. Источники: каталог, адаптеры, поколения (F13, F21, F27, дефект 22)

`collect()` больше не делает `source_spec(url)` по плоскому списку.

* `SourcePlan` + `catalog_source_plans()` + `source_plan_of()`: выборка
  пользователя разрешается через `source_catalog.migrate_settings` и
  `materialize_selection`, когда `--sources` не задан.
* `SOURCE_KINDS` дополнен `json-records`, `fields`, `page-json`, `html-table` —
  31 запись каталога, которые были недостижимы. Тело читает
  `source_adapters.parse_page` с **профилем из каталога**, а не с угадыванием.
  Записи приходят в `add()` с ключами `value` / `values` / `declared`, поэтому
  `declared.country` сохраняется как **заявление издателя**
  (`country_source='source'`), а не как измерение.
* `record_source_observation` / `record_source_generation` / `record_source_state`
  / `record_source_identity` пишут в таблицы миграции 16: наблюдение со
  счётчиками, **поколение** (неизменяемый ответ), `source_state` с ETag,
  Last-Modified, backoff и карантином; `membership_source` — происхождение.
* 304: `If-None-Match` / `If-Modified-Since` из `source_state`; на 304 **не**
  создаётся новое поколение и не пишется новое тело (`cache_state='not_modified'`,
  `last_304_at`).
* Карантин: три подряд отказа → источник не опрашивается вовсе до
  `quarantine_until`; backoff 60 → 300 → 1800 → 7200 → 21600 с.
* `source_contributions(db, ids)` — функция, которую
  `source_management.attach_contributions` ищет по имени.
* `source_spec()` вернул **двухзначный** кортеж, как и раньше; планы едут
  отдельно, чтобы старые распаковки не сломались.

**CLI `source`**: `list | show | sets | set | enable | disable | add | remove |
check | update | recover | status | exclude-scope | health | redact`
(было `list | health | redact`).

**Живой прогон, все пять адаптеров, профили из встроенного каталога:**

```
=== collect through EVERY adapter ===
  OK  src-fields         http=http_2xx_nonempty  parse=complete  rows=2 accepted=2
  OK  src-html-table     http=http_2xx_nonempty  parse=complete  rows=2 accepted=2
  OK  src-json-records   http=http_2xx_nonempty  parse=complete  rows=2 accepted=3
  OK  src-line           http=http_2xx_nonempty  parse=complete  rows=3 accepted=3
  OK  src-page-json      http=http_2xx_nonempty  parse=complete  rows=1 accepted=1
  generations=5 observations=5 identities=5 membership_source=11
=== second pass (conditional request) ===
  все пять: http=not_modified cache=not_modified rows=0
  304 answered: 5 | generations before/after: 5 / 5 | equal: True
  sources with last_304_at: 5
=== empty 200 ===        http=empty_body parse=empty
=== broken document ===  http=http_error  parse=invalid error=SOURCE_INVALID_JSON
=== record cap ===       http=http_2xx_nonempty parse=budget_exceeded error=SOURCE_RECORD_LIMIT
=== failing source, attempt 3 === failures=3 quarantined=True backoff=True
=== failing source, attempt 4 === error=SOURCE_QUARANTINED
```

---

## 3. `/v1/sources` на каталоге (F13, F27)

* `_op_sources_catalog` отдаёт `source_management.build_view` — в каждой строке
  есть `support` (supported / needs_auth / needs_adapter / experimental /
  unsupported) и `dataset_group`, плюс `filters`, `facets`, `sets`, `access_groups`.
* `_op_sources_list` / `_get` — строки каталога с **общим** id-пространством и
  рантайм-снимком. Раньше был отдельный плоский список URL в `sources.json`:
  id из `/v1/sources/catalog` нельзя было ни включить, ни выключить, ни обновить.
* `create` / `update` работают через `source_catalog.custom_source` и
  `source_management.select_ids`; **обновить запись каталога нельзя** —
  `E_STATE_NOT_FOUND` с текстом «a catalog record describes a published list».
* `refresh/preview` и `refresh/{job_id}` читают **строку `source_feed`**
  (`_feed_state_of`). Источник, который ни разу не загружался, отвечает
  `has_feed_row: false, outcome: 'never_fetched'` — вместо синтетического
  состояния, собранного из последнего отчёта collect.

```
GET /v1/sources/catalog -> 200 total = 150
   row has support       : True -> unsupported
   row has dataset_group : True -> 3proxy-self-hosted
GET /v1/sources -> 200 items = 150
GET /v1/sources/{id} -> 200 | support = unsupported | dataset_group = 3proxy-self-hosted
POST /v1/sources -> 200 custom-9e5600e2d9d29a9d custom=True
POST disable -> 200 False | enable -> 200 True
POST refresh/preview -> 200 | has_feed_row = False | outcome = never_fetched
PATCH /v1/sources/{catalog-id} -> 404 E_STATE_NOT_FOUND
```

**Просит владельца `source_management.py`:** `read_settings` / `write_settings`
вызывают `gui.read_settings` / `gui.save_settings`, которых в модуле нет (GUI
держит их как `App.settings` / `App.save`). Любой вызов этих двух функций падает с
`AttributeError`. `proxytool.write_source_settings` поэтому пишет файл сам — с
тем же `gui.validate` и тем же атомарным писателем.

---

## 4. Задания и пулы (F11, F14, дефекты 6 и 12)

* `POST /v1/pools/{id}/recheck` **исполняется**. `proxytool.run_pool_recheck()`
  создаёт задание, стартует его и гоняет `scan` с `job_id`/`job_store` этого
  задания; наблюдения, состояния элементов и события принадлежат именно ему.
  Повтор того же `Idempotency-Key` возвращает уже завершённое задание, а не
  запускает второе. Раньше `kind='pool_recheck'` не имел **ни одного**
  потребителя в дереве.
  `want`/`count_what` едут из `PoolStatus.find` — единица пула, а не константа.
* Ответу добавлено `job_id`: маршрут объявлен `async_job=True`, а `_job_dict`
  отдаёт `id`, поэтому **любой** `POST /v1/jobs/{id}/retry` и `pools/{id}/recheck`
  раньше падал с `503 'a long operation must return a job id'`.
* `pool_candidate_source` теперь отдаёт `exit_ip`, `country`, `asn` из
  `results.payload`. Без них квота `{'exit_ip': 1}` на живом API-пути была
  декоративной: все кандидаты приходили с `exit_ip=None`.
* CLI `pool refill | recheck` (было `list | create | status | members`).

```
pool pool-a: serving 0 of 2 (empty) — E_TIME_TTL_MISSING      # refill, честный отказ
pool pool-a: job job-8855dd86db90d0e4, checked 1, passed 0 (succeeded)   # recheck
```

### Решение по `jobs.retry()` (дефект D, area-jobs §7)

`JobStore.retry()` по умолчанию берёт всё, что не `done`, и **включает
`blocked`** — то есть адреса, помеченные `OBSOLETE_MEMBERSHIP` при выходе из
коллекции. Менять это в `jobs.py` нельзя, и ломать
`tests/test_jobs_events.py::HistoryTests::test_the_store_never_writes_outside_its_own_four_tables`
(он вызывает `retry` на задании, чьи единственные незавершённые элементы — как раз
`blocked`) тоже нельзя.

**Решение: `retry` не имеет права расширять область.** Адрес, вышедший из
коллекции, — это не «недоделанная работа», это работа, которой больше нет в
области; честный ответ для него — отчёт, а не сокет. Поэтому `api._op_jobs_retry`
собирает список элементов сам, исключает `blocked` и называет их количество в
ответе (`excluded_blocked`). Если незавершённых элементов, кроме `blocked`, не
осталось — `409 E_CONFLICT_REVISION` с `blocked_only: true` и указанием
`jobs/{id}/recover` (эта операция перезапускает именно незавершённые элементы).

```
items before retry: {'d9e97': ('done', None), '53927': ('blocked', 'OBSOLETE_MEMBERSHIP'),
                     '180e5': ('probing', None)}
retry -> 202 | new job job-16b349d17e91ed90 excluded_blocked = 1
queued: ['180e5']
the endpoint that left the scope is NOT queued: True
```

**Просит владельца `jobs.py`:** перенести это правило в сам `retry` (исключить
`blocked` из выборки по умолчанию), тогда `api.py` сможет отказаться от
собственной фильтрации, а `tests/test_jobs_events.py` — обновить.

---

## 5. Секрет ключа показывается один раз (CONTRACTS §5.1, дефект 18)

`apiv1._invoke` кладёт в кэш идемпотентности **тот же объект `Response`**, что
возвращает клиенту, а `Response` — frozen dataclass с `body: bytes`, уже
сериализованным. Маркер `OneShotBody` к этому моменту потерян, поэтому повтор того
же `POST /v1/keys` с тем же `Idempotency-Key` отдавал полный секрет второй раз.

Правка — одна строка рядом с `self.idempotency.put(...)`:
`apikeys.without_one_shot_response(response)`. Функция уже была готова, просто
не вызывалась.

```
WITHOUT the fix:  first  -> 200 secret=pwk_8fc03b0cbb17_XAygiXS5yYdwQ2CvJSQmJfGAL-C8486ch9Dp2l22IVE
                   replay -> 200 secret=pwk_8fc03b0cbb17_... (тот же)  SAME SECRET TWICE: True
WITH the fix:     first  -> 200 id=6b5f45ef0d40fc4a secret=pwk_606819dbe2e7_...
                   replay -> 200 id=6b5f45ef0d40fc4a secret=absent secret_already_shown=True
                   SAME SECRET TWICE: False | IDEMPOTENT: True | rows: 1
```

---

## 6. Диагностика: `diagnose health` больше не кричит «5 checks, 5 problems»

`HealthCheck.to_dict()` отдаёт булево `ok`; поля `state` нет. Фильтр читал
`item.get('state') != 'ok'` — `None != 'ok'` истинно для каждой проверки.
Теперь читается `ok`, а `state` оставлен как псевдоним.

```
$ proxy-workbench --data <fresh> diagnose health
health: 5 checks, 1 problems      # было 5 checks, 5 problems
```

---

## 7. Экспорт: `--client-target` / `--client-binary` (F20, area-export §2)

Флагов не было, поэтому версионная проверка совместимости sing-box не
выполнялась, и пользователю уходил непроверенный файл (а при пустой выборке —
вообще не писался, с `E_EXPORT_TARGET_UNPINNED` в статусе). Оба флага добавлены
рядом с `--credentials` и прокинуты в `export(...)`, где
`exportsvc.ExportOptions` их уже принимал. Значения по умолчанию берутся из
`PROXY_WORKBENCH_SINGBOX_TARGET` / `PROXY_WORKBENCH_SINGBOX_BIN`.

---

## 8. География: паритет экспорта и отбора (F08)

`core.Policy` выражал только `countries: frozenset` + `unknown_country` из трёх
значений, поэтому `basis=exit`, «никогда здесь» и честная политика `unknown` были
недостижимы, и экспорт расходился с GUI/API.

* `core.Policy` получил `country_criterion` (объект `geo.CountryCriterion`,
  **передаётся**, а не строится: `core` остаётся leaf) и `exit_country_of`.
* `_country_criterion_reason()` зовёт `geo.evaluate` — тот же вызов, что делают
  GUI-фильтр и движок экспорта.
* `proxytool.export` и `proxytool.scan` строят критерий одним вызовом
  `country_criterion(...)`; `matches_selection` принял `criterion`/`exit_country_of`.
* CLI: `--country-basis {endpoint,exit,either}`, `--country-exclude`,
  `--country-unknown {exclude,include_unverified,require_measurement}`.
* `unknown` больше не приравнивается к отказу: `include_unverified` и
  `require_measurement` **допускают** строку и говорят, чего о ней не известно.
* Старый `countries=` работает как раньше (`country_criterion=None` → прежний путь).

```
endpoint basis, want NL, DE endpoint   -> E_SCOPE_COUNTRY basis=endpoint reason=geo_country_not_included
exit basis, want NL, DE end + NL exit  -> admitted=True
exit basis, want NL, DE end + DE exit  -> E_SCOPE_COUNTRY basis=exit reason=geo_country_not_included
exclude DE, exit=DE                   -> E_SCOPE_COUNTRY reason=geo_country_excluded
unknown + include_unverified          -> admitted=True
unknown + exclude (default)           -> E_SCOPE_COUNTRY reason=geo_country_unknown_excluded
legacy countries= (unchanged path)    -> E_SCOPE_COUNTRY
```

---

## 9. Импорт: политика на всех путях + хранилище секретов (дефект 10, F04)

* `api._import_policy` читает явное поле `allow_private_endpoints` и пишет выбор
  в аудит (`import.policy`). Поле объявлено в контракте маршрута
  (`apiv1.py`, `openapi.json` перегенерирован).
* `importer.EndpointPolicy` переименован в `DestinationPolicy` и получил вторую
  ось `credentials: refuse | store`; `EndpointPolicy` остаётся именем того же
  класса, чтобы CLI и GUI не сломались. Комментарий «there is no secret store
  yet» был устаревшим — `secrets.AccessStore` есть.
* `credentials='store'` снимает userinfo из адреса (адрес попадает в коллекцию
  без секрета), а значение уходит в `secrets.AccessStore.create` — свой
  `access_id`, своя ревизия, свой верификатор. В предпросмотре значение не
  появляется даже флагом `has_credentials`.
* Комментарий «файл с колонкой пароля отвергает весь файл» сохранён для политики
  отказа; при `store` колонка и userinfo обрабатываются одинаково.

```
DEFAULT (public only)     line=1 E_IMPORT_HOSTNAME  line=2 E_IMPORT_PRIVATE  line=3 E_IMPORT_CREDENTIALS
private allowed           line=1 valid  line=2 valid  line=3 E_IMPORT_CREDENTIALS
private + store           line=1 valid  line=2 valid  line=3 valid
  commit counts: {... 'added': 3, 'credentials_stored': 1}
  accesses: [('0d7f9d83a8624b75', 'http_basic', 'sec_42e3363581a7a4b9e4cea7abfcd59a0d')]
  stored address has no userinfo: ['http://203.0.113.9:3128']
  password in report on disk: False
```

---

## 10. Пауза расписания переживает правку интервала (F15, area-ddl §6.5)

`SqliteScheduleStore.save_spec` писал `INSERT OR REPLACE`, а это в SQLite
`DELETE` + `INSERT`: колонки, которых нет в списке, получают значение по
умолчанию. `paused`, `pause_reason`, `resume_at`, `counters_json`,
`last_run_at` в списке не было — пауза и дневной счётчик **молча обнулялись**,
когда пользователь правил интервал. Заменено на
`INSERT … ON CONFLICT(id) DO UPDATE SET` по тем же колонкам.

```
after save_state : (1, '{"requests": 3, …}', 1777888800.0)
after save_spec  : (1, '{"requests": 3, …}', 1777888800.0)   <- было (0, None, 120.0)
interval kept    : (120.0,)
after reopen     : (1, 'paused_user', '{"requests": 3, …}')
```

---

## 11. `desktop.FALLBACK_SCHEMA_VERSION` → 18

Миррор `db.SCHEMA_VERSION`; теперь равен.

---

## 12. Производительность: кэш колонок `endpoints` (area-ddl / fix-engine §4)

`db.upsert_endpoint` звал `db.columns()` (PRAGMA) на **каждом** вызове:
500 020 раз на 50 000 результатов. `db.py` не мой, поэтому кэш сделан на стороне
движка: `proxytool.endpoint_columns(conn)` читает набор колонок один раз на
соединение и перепроверяет его по `PRAGMA schema_version` (миграция, случившаяся
при открытом соединении, инвалидирует кэш сама). `proxytool.upsert_endpoint`
выполняет тот же оператор, что и модуль, и тест сверяет результат построчно.

```
db.upsert_endpoint  5000 calls: 0.123 s  (24.6 us/call)
cached equivalent   5000 calls: 0.052 s  (10.5 us/call)   speedup 2.3x
same endpoint ids: True | rows equal: True 5000 5000
```

**Просит владельца `db.py`:** перенести кэш в `db.columns` (модуль-уровень, по
`id(conn)` + `schema_version`) и вернуть `upsert_endpoint` к модулю. Тогда
`proxytool.upsert_endpoint` можно удалить, а выигрыш получат все вызывающие, а не
только движок.

---

## 13. `EXPENSIVE_UNTIL_N` или `EXPENSIVE_ALL_PASSING` — решение

**Оставлено `EXPENSIVE_ALL_PASSING`**, и это осознанный выбор, а не дефолт.

`EXPENSIVE_UNTIL_N` перестаёт платить за judge и замер скорости, как только
запрошенное N достигнуто. Для прогона, чья единственная цель «дайте мне N», это
правильная политика. Для этого продукта — нет: `--min-anonymity` — это **фильтр**,
а не цель. Строка, пропустившая дорогую стадию, вообще не имеет вердикта
анонимности, поэтому `core.Policy` ранжирует её как `unknown` и **выбрасывает из
экспорта**. Корпус из 4000 адресов, измеренный с `--want 5`, дал бы 5 elite и
3995 молчаливых отказов — и фильтр выглядел бы так, будто он отверг 3995 адресов.
Под `ALL_PASSING` каждый прошедший базовую стадию получает настоящий уровень, и
экспорт — это «elite-адреса», а не «первые пять измеренных».

Цена реальна — один запрос к judge и одно тело на каждый прошедший адрес, — и
именно для неё существуют `--max-requests`, `--deadline`, `--judge-concurrency` и
покомпонентные бюджеты. Прогон, остановившийся по бюджету, оставляет
неизмеренные строки **без вердикта вообще**, а не с неверным.

Политика теперь видна в `run_state['expensive_policy']` и
`run_state['run_expensive']`.

---

## 14. Что осталось за чужими файлами

| # | файл | что нужно |
|---|---|---|
| 1 | `source_management.py` | `read_settings`/`write_settings` зовут `gui.read_settings`/`gui.save_settings`, которых нет. Любой вызов — `AttributeError`. Либо переименовать в `App.settings`/`App.save`, либо перенести запись в GUI-модуль. |
| 2 | `jobs.py` | `JobStore.retry()` исключает `blocked` из выборки по умолчанию (см. §4), после чего `api._op_jobs_retry` упрощается, а `tests/test_jobs_events.py` обновляется. |
| 3 | `db.py` | кэш колонок в `db.columns` вместо обходного пути в `proxytool` (см. §12). |
| 4 | `db.py` | `test_db_retention::test_vacuum_is_opt_in` падает из-за роста схемы 56 → 79 страниц (area-ddl §5.3 п. 3). Либо порог, либо отказ от части индексов на пустых таблицах. |
| 5 | `gateway.py` | `test_gateway_rotation::test_random_spreads_over_every_candidate` нестабилен по построению: 9 розыгрышей из 3 кандидатов, вероятность провала ≈ 7.8 % (fix-engine §2.3). Нужен seed или другая формулировка. |
| 6 | `tests/test_web_connect.py` | `gui.gateway_state` читает `runner.gateway.reachable_from_lan`; у тестового `FakeRunner` поля `gateway` нет. Падает на коммите `d917d91` **без** моих правок — владелец `gui.py`/`tests`. |
| 7 | `exportsvc.py` | `EXPORT_CODES` не попадают в `diagnostics.help_entries()`; модули не связаны намеренно. Для страницы помощи по экспорту нужна регистрация или отдельная секция. |
| 8 | `diagnostics.py`/`packaging` | `tests/test_extras::SingboxTests::test_config` падает на чистом HEAD (проверено на `4349b53` и на `d917d91`), нестабильность окружения, не регрессия. |
| 9 | `paths.py` | Правок не потребовал. Но: `__main__.resolve` отправляет `--data X --help` в desktop-хост, потому что `--data` в `DESKTOP_ARGS`. Починено в `desktop.main` (мой файл) — `--help`/`--version` всегда отвечают. Саму маршрутизацию разумно перенести в `__main__`. |
| 10 | `gui.py`, `ui/*` | `--client-target` в форме настроек; `hosting_basis` рядом с `hosting` в строке результата; выбор коллекции и `--allow-private-endpoints` в воркере (integrator.md §4.1, ещё не сделано). |

---

## 15. Проверки

```
.venv/bin/python -m unittest discover -s tests
  Ran 3244 tests  FAILED (failures=2, errors=2, skipped=1)

.venv/bin/python functional_acceptance.py
  === ИТОГ === done 30        exit 0

.venv/bin/python -m unittest tests.test_integration_end_to_end \
    tests.test_integration_transport tests.test_integration_contracts
  Ran 27 tests — OK
```

Чистый HEAD `4349b53` для сравнения: `Ran 3216 tests, failures=9, errors=3`.
Восемь из двенадцати закрыты этой интеграцией (см. §16).

### 15.1 Сквозной путь на моках, фактический вывод

`tests/test_integration_end_to_end.py` — один прогон, все двери, в порядке, в
котором их встречает человек. Настоящий HTTP-прокси с CONNECT на 127.0.0.1,
настоящий origin, отдающий все пять форматов источников и judge; ни одного
публичного адреса, ни одного реального списка.

```
collect через 5 адаптеров     OK x5, generations=5 observations=5 identities=5
второй collect                304 x5, поколений 5 -> 5
пустой 200                    http=empty_body parse=empty
scan                          state=complete, checked=1, evidence=transfer_ok,
                              строка несёт anonymity (с code) и speed
export                        exported=1, 14 файлов артефакта,
                              singbox.json без устаревшего outbound `block`
backup create                 sha256 есть, файл на диске
pool create + refill          state=empty, served=0, deficit E_TIME_TTL_MISSING
schedule add                  persistence()['lost_on_restart'] == []
api-key bootstrap             200
GET /v1/sources/catalog        200, 150 строк, support + dataset_group
POST /v1/pools/{id}/recheck   202, job_state=succeeded
POST /v1/keys (idem)           200 + секрет
POST /v1/keys (тот же idem)    200, секрета нет, secret_already_shown=true
gateway                       слушает порт, pool.refresh() отвечает
```

### 15.2 Переписанные тесты, что было сломано и почему новое поведение верно

1. **`tests/test_probes_reference.py::test_unmeasured_transports_are_declared_unsupported`**
   утверждал `supported=False` для `websocket_handshake`,
   `long_lived_connection`, `media_manifest_segment` — после того, как для
   каждого появились настоящий endpoint, бюджет и исход (F20). Тест закреплял
   **отсутствие**, а не честность. Заменён на
   `test_measured_capabilities_name_their_endpoint_budget_and_outcome`; три
   неизмеряемых (`udp_transport`, `http2_or_http3`, `calls_video_any_service`)
   остались `supported=False`, но теперь проверяется и их outcome.
2. **`tests/test_areajobs_scheduler.py::test_a_pause_and_a_spent_budget_are_reported_as_lost_when_the_schema_cannot_hold_them`**
   называл дефект своим именем и утверждал
   `assertFalse(after.state('nightly').paused, 'this is the defect being reported')`.
   Миграция 17 добавила колонки, `ON CONFLICT` закрыл вторую дверь — дефекта
   больше нет, и утверждение теперь требовало бы бага. Переименован в
   `..._survive_a_restart_and_a_spec_edit` и проверяет **оба** прохода.
3. **`tests/test_db_migrations.py::CONTRACT_TABLES`** не знал про восемь таблиц
   миграций 16 и 18, поэтому «новая база — ровно контракт» стало ложью сразу
   после того, как контракт вырос (area-ddl §6.1 просит ровно эту правку).
4. **`tests/test_sourcedesk_compare.py::FetchStateTests.setUp`** исполнял
   `sourcedesk.REQUESTED_DDL` поверх `db.open_db()`, который с миграцией 16 уже
   создаёт `source_feed` — `OperationalError: table source_feed already exists`.
   DDL объявлен, а не исполняется; тождество схем проверяет
   `tests/test_area_ddl_sourcedesk.py`.
5. **`tests/test_paths.py::test_entry_point_dispatch`** ждал, что пустая командная
   строка открывает интерфейс. Это было верно до фонового слоя и перестало быть
   верным, когда поставленной точкой входа стал desktop-хост (F22/F23):
   приложение, которое пользователь открывает двойным щелчком, владеет меню,
   единственным экземпляром и автозапуском; страница без окна умирает вместе с
   вкладкой. `gui` остаётся способом попросить страницу одну.
6. **`tests/test_bandwidth.py::test_working_proxy_gets_a_speed`** требовал
   `mbps > 0` у 100 КБ loopback-передачи. Окно такой передачи много короче
   минимума 0.25 с, и `probes.measure_speed` теперь говорит `insufficient` и
   **не даёт числа** (дефект 15). Утверждение закрепляло старую арифметику,
   делившую реальный объём на окно, слишком короткое, чтобы что-то значить.
   Заменено на два теста: «слишком быстро — числа нет» и «окно достаточно длинное
   — число есть и считается от первого байта до последнего».
7. **`tests/test_anonymity.py::test_detect_own_ips_from_direct_judge`** требовал
   `ValueError`, когда judge отвечает только `10.1.2.3`. Дефолт
   `global_only=True` верен для публичного judge и неверен для self-hosted: judge
   на той же машине отвечает loopback-адресом, и его отбрасывание оставляло
   baseline пустым — тогда любой адрес выглядел `elite` за неимением сравнения.
   Тест переписан: LAN-адрес стал baseline, а отказ остался там, где judge не
   показал **никакого** адреса.
8. **`tests/test_importer_*.py` / `test_areacatalog_importer.py`** — падали не
   сами по себе, а из-за моей первой (неверной) правки отказа в
   `importer._record`: вместе с колонкой потерялась вторая половина условия
   (userinfo в самом адресе). Условие восстановлено целиком; при `credentials='store'`
   обе формы одинаково разбираются в `_classify`.

### 15.3 Оставшиеся четыре падения — не мои

* `test_extras.SingboxTests.test_config` — падает на чистом `4349b53` и на
  `d917d91` без единой моей правки (нестабильность окружения, отмечена в
  `area-export §8` и `area-probes §4.4`).
* `test_gateway_rotation.RotationTests.test_random_spreads_over_every_candidate` —
  нестабилен по построению (≈7.8 % провала), `gateway.py` не мой.
* `test_db_retention.RetentionTests.test_vacuum_is_opt_in` — схема выросла с 56
  до 79 страниц, `db.py` не мой (п. 4 в §14).
* `test_web_connect.QrRoundTripTests.test_the_qr_never_carries_the_gui_or_api_secret` —
  проверено на worktree коммита `d917d91`: падает там без моих файлов. Владелец
  `gui.py` / `tests/test_web_connect.py` (п. 6 в §14).
