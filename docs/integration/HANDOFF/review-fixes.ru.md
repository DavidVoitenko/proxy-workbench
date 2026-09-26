# Ремонт по находкам независимого ревью

Документ описывает, что было **воспроизведено**, что **исправлено** в ветке
`integration/ultra-2026-09-25` и что **осталось открытым** после цикла ревью из 25
подтверждённых находок. Для каждой строки «исправлено» указаны файл и суть причины;
для каждой строки «воспроизведено» — команда, которая давала дефект до исправления.

Правило документа: **невыполненная проверка написана как невыполненная.** Здесь нет
строки «проверено», под которой не стояла бы реально выполненная команда.

Коммиты: `01faf77`, `e8dd499`, `502ca37` и коммит с документацией.

---

## 1. Исправлено

### 1.1 F28 / F09 / дефект 3 / §7.10 — поколение было множеством измерений

**Воспроизведено** (`.venv/bin/python /tmp/rfx/repro_dup.py`, до исправления):

```
rows in results: 2
export checked/admitted: 2 2
ranked.json entries: [('job1', 5), ('job2', 50)]
proxies.txt: 'http://1.1.1.1:8080\nhttp://1.1.1.1:8080\n'
http.txt: '1.1.1.1:8080\n1.1.1.1:8080\n'
```

**Воспроизведено** (`.venv/bin/python /tmp/rfx/repro_mixed.py`, до исправления) — свежий
провал не отменял старый успех:

```
admission_counts: {'E_STATE_MEASUREMENT_FAILED': 1, 'E_TIME_TTL_EXPIRED': 1}
proxies.txt: 'http://1.1.1.1:8080\nhttp://2.2.2.2:8080\n'
ranked.json: [('http://1.1.1.1:8080', 'job1'), ('http://2.2.2.2:8080', 'job3')]
```

**Причина.** Миграция 13 вводила `job_id` в `PRIMARY KEY results`, поэтому одна конечная
точка имела по строке на каждое задание. Ни один потребитель не приводил набор к «одна
строка на адрес», и каждая строка судилась независимо.

**Исправление.** `proxy_workbench/db.py`: миграция 15 `results_one_row_per_address` сужает
ключ до `(profile_id, profile_revision, access_id, access_revision, endpoint_id)`. `job_id`
остаётся колонкой последнего измерения, per-job запись живёт в `job_item(job_id, item_id)`,
поэтому утверждение §6.3 «повторная проверка в новом job создаёт новый item» остаётся
верным без `job_id` в ключе. Схлопнутая история суммируется (`_collapse_payload`), ничего
измеренного не теряется; индексы миграции 14 пересоздаются после переименования таблицы.
`SCHEMA_VERSION` 14 → 15, `desktop.FALLBACK_SCHEMA_VERSION` синхронизирован.

**Проверка после:** те же два скрипта дают `rows in results: 1`, `ranked.json: [('job2', 50)]`
и `proxies.txt: 'http://1.1.1.1:8080\n'`; во втором — `{'E_STATE_MEASUREMENT_FAILED': 1}` и
адрес, который только что отвергли, в артефакте отсутствует.

### 1.2 F24 + F02 — backfill не доходил до проверяемого слоя

**Воспроизведено** (`.venv/bin/python /tmp/rfx/repro_legacy.py`, файл формы 2.x по DDL
`git show b39c7bf:proxytool.py`): до исправления экспорт historic-строки давал
`{'state': 'empty', 'admission_counts': {'E_SCOPE_COLLECTION': 1}}` — отказ по коллекции,
в которой строка никогда не измерялась.

**Причина.** Миграция 13 писала восстановленное происхождение в колонки
`profile_id`/`endpoint_id`/`access_id`, которые `export()` и таблица GUI не читают: они
разбирают только `results.payload`.

**Исправление.** `proxy_workbench/db.py`: `_backfill_payload_identity` доносит
восстановленное происхождение в payload и правит формулировку докстринга миграции 13.
Восстанавливается только то, что файл 2.x действительно содержал: `proxy`, `profile_id`,
`profile_revision = 1` (у профиля 2.x была одна ревизия), публичная беспарольная
идентичность доступа (пароля в формате не было) и `legacy-collected` для адресов из
`candidates`. `network_id` и `valid_until` **не выдумываются**: файл их не содержал.
Теперь отказ называет настоящую причину — `E_SCOPE_NETWORK`.

### 1.3 F24 — резервная копия только для `user_version == 0`

**Воспроизведено** (`.venv/bin/python /tmp/rfx/repro_backup.py`): база, помеченная
`user_version = 13`, мигрировалась с `backup None`, а каталог `backups/` не создавался.

**Исправление.** `proxy_workbench/db.py`: решение о копии принимается по **миграции**, а не
по виду файла. `DESTRUCTIVE_MIGRATIONS = frozenset({13, 15})`; копия с manifest берётся перед
любой неаддитивной миграцией при любом `user_version`.

**Проверка:** `migrated from 13 -> applied ((13,'results_rebuild'),(14,'indexes'),(15,'results_one_row_per_address'))`,
`backup …/proxies.sqlite3.v13.pre-migration.….bak`, `list_backups` её находит, `rows after migration: 1`.

### 1.4 F24 — retention по умолчанию падала

**Воспроизведено** (`.venv/bin/python /tmp/rfx/repro_retention.py`, до исправления):
`retention_preview` отчитывался об удалении обеих строк, `apply_retention` бросал
`E_DATA_MIGRATION_FAILED: FOREIGN KEY constraint failed`, удалено 0.

**Исправление.** `proxy_workbench/db.py`: `_retention_order` строит порядок по живым
внешним ключам (`_referrers` читает `PRAGMA foreign_key_list`), а не по рукописному списку.
Политика, которая называет родительскую таблицу, но не дочернюю, теперь отказывается явно:
`cannot delete from observations: results still point at it; add results to include`.

**Проверка:** `apply deleted: (('results', 1), ('observations', 1))`, строк не осталось;
политика только по `observations` даёт осмысленный отказ вместо голого sqlite-сообщения.

### 1.5 F29 — обход scope на артефактах экспорта

**Воспроизведено:** `GET /v1/exports/<чужая коллекция>` и `…/download/proxies.txt` с ключом,
ограниченным другой коллекцией, отвечали 200 и отдавали файл. Причина: у трёх маршрутов не
было `scope=`, а `_artifact()` фильтровал только по первичному ключу; `_guard_scope` смотрел
только на верхний уровень ответа, а сервис возвращает `{'item': {...}}`. Для
`raw_body=True` байты возвращались **до** `_redact`/`_guard_scope`.

**Исправление.** `proxy_workbench/api.py`: `_guarded_artifact` проверяет `collection_id` через
`_guard_objects`; `proxy_workbench/apiv1.py`: `_guard_scope` заходит в конверты `item`/`items`
(явный список, глубина один), для `raw_body` байты уходят только после проверки, а чужой объект
в **списке** выбрасывается, а не роняет всю страницу.

**Проверка:** `tests/test_apiv1_scope.py` (10) — три маршрута отвечают 404 ключу чужой
коллекции, своя коллекция по-прежнему читается, traversal и не-manifest по-прежнему 404.

### 1.6 F29 — список заданий/профилей/источников/расписаний не фильтровался

**Воспроизведено:** `_scope_values(principal, 'jobs')` возвращал `None` для вида, которого у
`Principal` нет, и `None` читается как «ключ не ограничен» — `GET /v1/jobs` отдавал чужое
задание целиком.

**Исправление.** `proxy_workbench/api.py`: `SCOPE_KINDS = ('collections', 'pools')`; для любого
другого вида `_scope_values` отвечает множеством коллекций (единственное, чем такой объект
может быть внутри), а `_guard_objects` фильтрует по собственной коллекции объекта. Общее
конфигурационное (профили, источники) ключ, ограниченный коллекцией, не получает — источник
отдаёт URL провайдера дословно, вместе с возможными учётными данными.

### 1.7 F29 — `include_secrets` был неработоспособен

**Воспроизведено:** `POST /v1/exports` с `include_secrets: true` и правом `export.secret`
отвечал `500 E_SERVICE_UNAVAILABLE` с текстом «the operation is not wired to the service
layer».

**Причина.** `api.py` передавал `credentials='include'`, значения нет в
`exportsvc.CREDENTIALS_MODES = ('redact', 'reference')`, поэтому `ExportOptions` отвергал
запрос уже после успешной проверки права.

**Исправление.** `proxy_workbench/api.py` передаёт `exportsvc.CREDENTIALS_REFERENCE` и
`SecretGrant`; `proxytool.export` получил параметр `secret_grant` и передаёт его в
`write_snapshot`; в CLI добавлен `--credentials {redact,reference}`. `apiv1._domain_refusal`
переводит доменный отказ в его собственный код, а не в «сломанную службу».

**Проверка:** `tests/test_exportsvc_secrets_export.py` (7) — 202 вместо 500, в артефакте
`access:acc-1@2` и никакого значения, `'include'` отсутствует в `CREDENTIALS_MODES`, canary
не встречается ни в БД, ни в файлах.

### 1.8 дефект 18 / R12 — пароль шлюза был равен токену API

**Воспроизведено** по коду и `compose.yml`: `proxytool.run_gateway` передавал
`args.api_token` в `gateway.start` как пароль ротирующего прокси, а `api.make_api_server` —
как bearer-токен read-only API; `compose.yml` задавал обеим службам одну
`PROXY_WORKBENCH_API_TOKEN`. Это прямо противоречит `gateway.new_gateway_token()`
(«never reads the API token») и `resolve_token`.

**Исправление.** `proxy_workbench/proxytool.py`: `--gateway-token` /
`PROXY_WORKBENCH_GATEWAY_TOKEN`; `run_gateway` его читает и **отказывается** работать, если
значение совпадает с `--api-token`. `compose.yml` использует обе переменные. GUI-путь уже был
исправлен ранее и не менялся.

**Не закрыто:** отдельного regression-теста на равенство нет — см. §3.

### 1.9 F14 / §7.13 — refill пула отвечал «ок» и ничего не делал

**Воспроизведено:** `POST /v1/pools/main/refill` отвечал 202 с
`{'collection_id': 'public-base', 'items': 0}` для пула над другой коллекцией; реальный
механизм (`pools.refill`, `pools.watch`, `Workbench.pool_refill`) не вызывался из продукта.

**Исправление.** `proxy_workbench/api.py`: `_op_pools_refill` вызывает
`workbench.pool_refill` для пула из `call.params['id']` с кандидатами из его собственной
коллекции (`pool_candidate_source`, вердикт — из общего контракта адмиссии, tier `sources`
выдаётся с `allowed=False` и попадает в `recheck_due`); `_op_pools_start` запускает пул;
`_op_pools_recheck` ставит в очередь коллекцию пула и **не принимает** `collection_id` из
тела. Маршрут `pools.refill` перестал быть `async_job` — он локален и ограничен.
Попутно исправлены три падения с 500: `pools_state_for` собирал `PoolStatus` вручную и
перестал работать после роста структуры (теперь `dataclasses.replace`), `save_status`
получал объект вместо строки состояния, `dict(member)` на dataclass `pools.Member`.

**Проверка:** `tests/test_pools_api.py` (11) — refill наполняет пул, тело не может
перенаправить refill/recheck, несуществующий пул даёт 404.

### 1.10 F29 — `/v1/results/{id}` и `/observations` не могли вернуть данные

**Воспроизведено:** `GET /v1/results/11.0.0.2:8080` → 404, `…/observations` → 200 с пустым
списком при существующей свежей строке.

**Причина.** `ID_PATTERN` не пропускает `://`, `_op_results_detail` сравнивал сегмент с
полным `proxy`, а `public_row` не публиковал `endpoint_id` вовсе; кроме того, чтелось
`exports.rows` без `load()`, то есть кэш на момент старта процесса.

**Исправление.** `proxy_workbench/api.py`: `public_row` публикует `endpoint_id`,
`proxytool.export` берёт его из колонки (`SELECT payload, endpoint_id`), добавлены
`_find_row` (endpoint_id / host:port / полный URL), `_guarded_row` (object-level scope) и
перечитывание снимка.

### 1.11 F12 / дефект 23 — `--max-requests` и `--run-max-bytes` были no-op

**Воспроизведено** (`.venv/bin/python /tmp/rfx/repro_budget.py`, до исправления):
`max_requests=1` → `probes actually run: 20 of 20`, `stop_reason='complete'`.

**Исправление.** `proxy_workbench/proxytool.py`: `gate.release(requests=…, bytes=…)`
начисляет фактически потраченное (`measurement_requests`, `measurement_bytes`); измерение,
не сделавшее ни одного запроса, не начисляется; `BudgetExhausted` больше не попадает в
`except Exception` (иначе «мы остановились» записывалось бы как «прокси мёртв»), а
останавливает прогон. `acquire(fds=1)` передаёт файловые дескрипторы явно.

**Проверка:** `max_requests=1` → `checked 1, stop_reason E_LIMIT_BUDGET`;
`tests.test_scan_budgets` (12) — оба лимита, плюс «отказ без запроса не останавливает прогон».

### 1.12 F12 — N endpoint, N уникальных IP и N exit-IP были одним числом

**Воспроизведено** (`.venv/bin/python /tmp/rfx/repro_count.py`, до исправления):
`count_what='ip'` и `count_what='exit'` давали одинаковый `found=0` при любых данных.

**Причина.** `counted()` возвращала `1 if row.get('exit_ip')` для обоих видов, а judge пишет
`anonymity.exit_ip` — ключ искался не там.

**Исправление.** `proxy_workbench/proxytool.py`: `ip` считает уникальные **хосты** адреса,
`exit` — подтверждённые адреса выхода (`exit_address_of`), все три счётчика наполняются
всегда и отдаются рядом; недобор `--want` сообщает `want_unreachable_<unit>` вместо
молчаливого `complete`.

### 1.13 F24 — restore/retention/cleanup/rebind были недостижимы

**Воспроизведено:** `grep` по продукту показывал, что `db.restore`, `db.retention_preview`,
`db.apply_retention`, `db.cleanup`, `db.rollback` и `db.rebind_secrets` вызываются только из
`tests/`; в CLI был только `backup create|list`, в `openapi.json` — ни одного из этих путей.

**Исправление.** `proxy_workbench/proxytool.py`: `backup verify|preview|restore|rollback|
retention|cleanup|rebind`; каждое изменяющее действие — двухшаговое (предпросмотр, затем
`--apply`). Новые флаги: `--apply`, `--to-data`, `--target`, `--retention-hours`,
`--include-fresh`, `--keep-newest`, `--vacuum`, `--keep-lock`, `--mapping`.

**Проверка:** `tests/test_scan_budgets.BackupCommandTests` (4) — все десять действий
возвращают 0, `restore --apply` кладёт базу и manifest в целевую папку, предпросмотр ничего
не меняет, `retention --apply` удаляет `{'results': 1, 'observations': 1}`.

### 1.14 F10 / F25 — диагностический слой был недостижим

**Воспроизведено:** `diagnostics.build_funnel`, `explain_zero`, `check_control`,
`build_bundle`, `health_report`, `fixture_recipe` встречались только в `tests/`; в продукте
модуль использовался как `diagnostics.classification(exc)`.

**Исправление.** `proxy_workbench/proxytool.py`: команда
`diagnose funnel|zero|control|health|bundle`. Воронка читает `status.json` текущей генерации
экспорта, а не указатель `current.json` (указатель не содержит `state`/`stop_reason`/
`exported`, и объяснение нулевого результата не срабатывало).

**Проверка:** `tests.test_diagnose_cli` (6). Живой вывод на базе с одной недостижимой строкой:
`diagnose zero` → `{"code": "UNREACHABLE", "stage": "tcp", "summary": "Address accepts no
connection", "action": "Check that the port is open…", "counters": {…}}`.

### 1.15 F26 / R20 — документация не соответствовала поставке

**Исправление.** `CHANGELOG.md` `[Unreleased]` заполнен реальными возможностями и
исправлениями; `PRODUCT_VERSION` 2.2.1 → **2.3.0**; `README.md` и `README.ru.md` получили
раздел «API и ключи: `/v1»» с bootstrap, curl/Python/JS-примерами, объяснением прав и
resource scope, и явно названное различие пароля шлюза и токена API; в обоих README
`--api-token` у шлюза заменён на `--gateway-token`. В CHANGELOG добавлен раздел
**Known gaps** с нереализованными F04, F21, F22, полным F12 и GUI-частями F03/F10/F25.

### 1.16 CONTRACTS §7.1 и §8 — таблица трассировки устарела, формулировка блокера F13 была неверна

**Исправление.** `docs/integration/CONTRACTS.ru.md`: §7.1 переписана по фактическому
состоянию (было 24 строки `todo` со снимком начала цикла), колонка «проверка» содержит
только реально выполненные команды; статус `verified` не присвоен ничему, потому что §7.4
требует четыре слота независимого ревью. §8.1 — исправлена причина блокера источников
(файлы `source_*.py` в дереве **есть**; не перенесён финальный handoff с двумя коммитами
`e1d57ce`, `8eca9a3`). §8.2 — исправлено утверждение, что `access_revision` не существует:
он есть (миграция 3, `core.Access`, `secrets.AccessStore`); настоящая причина F04 —
нереализованная аутентификация на прокси.

---

## 2. Изменённые тесты (контракт изменился — тест переписан, а не удалён)

| Файл | Что было | Стало |
| --- | --- | --- |
| `tests/test_db_migrations.py` | `read_header` ожидал 14; список миграций `range(15)`; ключ `RESULTS_NEW_KEY`; утверждение «повторная проверка в новом job — новая строка»; payload legacy-строки оставался `{"score": 80}` | версия из `db.SCHEMA_VERSION`; ключ `RESULTS_KEY`; повторная проверка **заменяет** строку; проверяется, что восстановленное происхождение дошло до payload, а `network_id`/`valid_until` остались пустыми |
| `tests/test_db_retention.py` | фикстура писала шесть разных `proxy` с **одним** `endpoint_id` и одним `access_id` | каждая строка получила свой endpoint и свой access; под новым ключом старая фикстура нарушала его смысл (шесть измерений одного адреса) |
| `tests/test_apiv1_control.py` | `POST /v1/pools/p1/refill` ожидал 202 | ожидает 200: refill локален, ограничен и отвечает статусом результата, а не job id, который ни на что не указывал. Отдельно: `assertTrue(running.is_set())` заменён на `running.wait(2)` — гонка с запуском потока фейка, а не проверка поведения |
| `proxy_workbench/openapi.json` | снимок таблицы маршрутов | перегенерирован из `apiv1.openapi_document()` после изменения деклараций `pools.refill`/`pools.recheck` |

---

## 3. Что осталось открытым

Ничего из перечисленного не помечено «почти готово».

### 3.1 F04 — аутентификация на прокси (не реализована, внешний объём)

`probes.py` и `proxytool.request_once` не умеют `Proxy-Authorization` и логин/пароль SOCKS5;
userinfo в URL отвергается намеренно (`proxytool.py:138`, `probes.py:589`). Поэтому
`results.access_id` всегда называет беспарольную публичную идентичность, а «смена пароля
отзывает старое доказательство» на сетевом пути не наблюдаемо. **Что сделано:** `as_access()`
принимает vault-ную запись `secrets.Access`, пару `(access_id, access_revision)` или `None` —
раньше реальная идентичность роняла измерение на первой строке с
`AttributeError: 'Access' object has no attribute 'access_id'`. **Что нужно:** транспорт
`Proxy-Authorization`/SOCKS5 auth в обоих движках проб, разрешение секрета перед пробой,
`--access-id` в CLI и выбор доступа в задании, и привязка шлюза к идентичности доступа вместо
текущего отказа «407 → прокси непригоден». **Владелец: `secrets.py` + `probes.py`.**
**Почему не сделано здесь:** это не дефект маршрутизации, а отсутствующая функция в двух
транспортных модулях; реализация без отдельного приёмочного сценария была бы «кнопкой без
работающего backend» (§8 запрещает).

### 3.2 F21 — сравнение источников и провайдеров (не реализовано)

В `sourcedesk.py` (1642 строки) нет ни сравнения, ни cohort, ни survival, ни overlap, ни
уникального вклада family, ни cost-per-admitted. `grep -rn "cohort|survival|overlap|
cost_per" proxy_workbench/*.py` — пусто. Требует отдельной карточки и собственных фикстур;
приёмка «одинаковые fixtures в другом порядке дают сопоставимый результат» не имеет ни
одного закрытого сценария. **Владелец: `sourcedesk.py`.**

### 3.3 F22 — фоновая работа: трей, автозапуск, sleep/wake (не реализовано)

`Scheduler.mark_wake` (`scheduler.py:2117`) и `jobs.mark_awake` (`jobs.py:1187`) существуют и
покрыты тестами, но из продукта их никто не зовёт: `grep -rn "mark_wake|mark_awake" --include=*.py .`
даёт только определения и тесты. Трей, меню-бар, различие close/quit, повторный запуск в
работающий экземпляр, opt-in автозапуск и реакция на сон/смену сети отсутствуют;
`docs/packaging/RELEASE-NOTES.ru.md` это фиксирует честно. **Владелец: `desktop.py`.**
**Промежуточный шаг, который можно взять отдельно:** GUI сейчас не сообщает планировщику о
пробуждении — один вызов `scheduler.mark_wake()` при старте окна закрыл бы «после сна нет
шквала catch-up» без платформенного слоя; это не сделано, потому что `gui.py` в этой сессии не
трогался (см. §4).

### 3.4 F12 — `Pipeline` / `run_pipeline` не выполняются продуктом

`proxytool.scan` делит с конвейером `Budgets`, `ResourceGate`, `Ledger`, `FindPolicy` и
`PriorIndex`, и после исправления 1.11/1.12 бюджеты и три N работают именно в продукте. Но
`Pipeline`, `run_pipeline`, `AdaptiveConcurrency`, `HostLimiter`, `TargetPolicy` и
`benchmark()` по-прежнему вызываются только из своих тестов: стадий cheap→basic→expensive в
продукте нет (есть prefilter + один вызов `probe`), адаптивной конкурентности нет (число
workers — фиксированный флаг), per-host/per-target лимиты не действуют, и потока
источник→проба нет (`collect` пишет всё в SQLite, `scan` читает кандидатов из БД).
Следствие: **бенчмарки `pipeline.benchmark` честно SYNTHETIC_NOTICE, но описывают цепочку,
которую программа не запускает, и цитировать их как характеристику продукта нельзя.**
Потолки `max_open_fds=256` и `max_ram_bytes=64MB` теперь передаются в `acquire(fds=1)`, но
`ram` по-прежнему не начисляется: для этого нужен учёт памяти на измерение.
**Владелец: `pipeline.py` + `proxytool.scan`.**

### 3.5 F03 (GUI-часть) — импортёр недостижим из интерфейса

`importer.py` (1162 строки) доступен из CLI (`import preview|commit|list`) и
`/v1/collections/{id}/imports`, но `proxy_workbench/gui.py` не содержит ни одного упоминания
`importer`/`import_preview`/`import_commit`; в интерфейсе есть textarea `#proxies` и
file-drop, которые пишут напрямую через `App.add_collection_members`
(`gui.py:1386-1426`). Нет preview, номеров отклонённых строк, mapping колонок,
merge/replace, отмены и идемпотентного commit. `index.html:1439` ограничивает `accept`
значением `.txt,.list,text/plain`. **Владелец GUI-поверхности.**

### 3.6 F10/F25 (GUI-часть) — панели диагностики в интерфейсе нет

Счётчики воронки, кнопка диагностического пакета и объяснение «почему 0 результатов»
появились в CLI (§1.14) и доступны по HTTP, но в `ui/` их нет
(`grep -c funnel proxy_workbench/ui/app.js` → 0). **Владелец GUI-поверхности.**

### 3.7 F29 (GUI-часть) — менеджер ключей в интерфейсе отсутствует

В `gui.py` среди ~60 маршрутов `/api/*` нет ни одного про ключи; в `ui/index.html` есть
только `meta[name=workbench-token]` и текст «key parameters». Управление ключами есть в CLI
(`api-key`) и в `/v1`. **Первая административная ключевая пара выдаётся только локальным
bootstrap** — GUI должен либо дать его на панели Help, либо явно сказать, что это делается
в CLI. **Владелец: `gui.py` + `ui/*`.**

### 3.8 дефект 18 — нет отдельного regression-теста на различие токенов

Само различие сделано (`--gateway-token`, отказ при совпадении значений, `compose.yml`), но
тест, который бы это зафиксировал, не написан. Причина честная: SOCKS5-хендшейк против
локального шлюза в тестовом наборе не поднимается, а поднимать его ради одной проверки —
значит написать интеграционный тест с сокетом, чего в этом наборе нет. **Запрос владельцу
`gateway.py`:** добавить `tests/test_gateway_token_identity.py`, который поднимает
`gateway.start` на loopback, делает SOCKS5-хендшейк паролем шлюза и тем же значением —
`Authorization: Bearer` — и проверяет, что второе не принимается (ожидается 401/403).

### 3.9 F03/F29 — почему GUI-часть не сделана в этой сессии

`proxy_workbench/ui/app.js` и `proxy_workbench/ui/style.css` на момент начала работы
содержали **незакоммиченные правки другого исполнителя** (добавление поля «Protocol» в
редактор сервисов). Правка `app.js` в этой сессии означала бы коммит чужой незавершённой
работы и риск конфликта в том же файле, поэтому GUI-часть F03, F10/F25 и F29 вынесена в
§3.5–3.7 владельцу поверхности, а не сделана «наполовину». Всё, что для этого нужно со стороны
продукта, сделано: маршруты `/v1/collections/{id}/imports`, `/v1/keys`, `/v1/diagnostics/*`
существуют и отвечают.

---

## 4. Проверки, выполненные в этой сессии

| Команда | Результат |
| --- | --- |
| `.venv/bin/python -m unittest discover -s tests` | `Ran 2437 tests … OK (skipped=1)` — финальный прогон, см. §5 |
| `.venv/bin/python -m unittest tests.test_apiv1_scope` | `Ran 10 tests … OK` |
| `.venv/bin/python -m unittest tests.test_exportsvc_secrets_export` | `Ran 7 tests … OK` |
| `.venv/bin/python -m unittest tests.test_pools_api` | `Ran 11 tests … OK` |
| `.venv/bin/python -m unittest tests.test_scan_budgets` | `Ran 12 tests … OK` |
| `.venv/bin/python -m unittest tests.test_diagnose_cli` | `Ran 6 tests … OK` |
| `.venv/bin/python -m unittest tests.test_db_migrations tests.test_db_backup tests.test_db_retention tests.test_db_fixtures tests.test_db_collections tests.test_db_write_guard tests.test_db_secrets` | `Ran 69 tests … OK` |
| `.venv/bin/python -m unittest tests.test_api tests.test_apiv1_auth tests.test_apiv1_control tests.test_apiv1_apikeys tests.test_apiv1_limits tests.test_apiv1_service tests.test_gateway` | `Ran 106 tests … OK` |
| `.venv/bin/python -m unittest tests.test_gui tests.test_selection` | `Ran 41 tests … OK` |
| `.venv/bin/python -m unittest tests.test_pipeline_chain tests.test_pipeline_limits tests.test_pipeline_bench tests.test_pipeline_findn tests.test_workbench` | `Ran 127 tests … OK` |
| `.venv/bin/python -m unittest tests.test_packaging_release tests.test_i18n` | `Ran 28 tests … OK` |
| `/tmp/rfx/repro_dup.py`, `/tmp/rfx/repro_mixed.py`, `/tmp/rfx/repro_legacy.py`, `/tmp/rfx/repro_backup.py`, `/tmp/rfx/repro_retention.py`, `/tmp/rfx/repro_retention2.py`, `/tmp/rfx/repro_budget.py`, `/tmp/rfx/repro_count.py` | воспроизведение и проверка после исправления; скрипты вне репозитория |

**Не выполнено:** GUI-проверка в браузере (нет в инструментах этой сессии), сборка
настольной поставки `.app`/`.dmg`, приёмочный сценарий `functional_acceptance.py` целиком
(запускался только его модуль через общий набор — приёмка приёмкой не считается).

---

## 5. Замечания для интегратора

1. **Схема базы теперь 15.** Любая база на 14 и ниже обновляется копией **перед**
   пересборкой `results`; `list_backups()` покажет эту копию, `rollback` её восстановит.
   `desktop.FALLBACK_SCHEMA_VERSION` и `openapi.json` за версией схемы не отстают.
2. **`PRODUCT_VERSION` стал 2.3.0.** `CHANGELOG.md` описывает 2.3.0 как `[Unreleased]` с
   перечнем исправлений и Known gaps. Если релиз собирается под другим номером, поправьте
   `branding.PRODUCT_VERSION` и заголовок секции вместе.
3. **Именованный scope — не «всё понятно».** Ограничение ключа коллекцией теперь
   распространяется на задания, профили, источники, расписания и артефакты экспорта. Если
   кому-то из потребителей нужен read-only доступ ко **всему** без коллекций, его надо
   выпустить **без** resource scope, а не с чужим.
4. **`proxytool backup` расширен.** Скрипты, разбиравшие вывод `backup list`, не затронуты;
   новые действия требуют `--apply` и явный `--to-data` — восстановление никогда не
   угадывает папку.
5. **Пулы.** `POST /v1/pools/{id}/refill` больше не 202, а 200. Клиент, который ждал job id
   для refill, должен читать `state`/`served` из ответа.
6. **Плановый прогон.** Полный набор падал в этой сессии трижды, каждый раз по одному
   тесту, и каждый раз проходил в изоляции и в следующем полном прогоне:
   `test_apiv1_control.ControlTests.test_long_operations_answer_202_and_do_not_hold_the_request`,
   `test_gateway_rotation.RotationTests.test_random_spreads_over_every_candidate` и
   `test_secrets_verifier.VerifierTests.test_comparison_does_not_stop_at_the_first_wrong_digest`.
   Первый оказался **гонкой в самом тесте**: `assertTrue(self.service.running.is_set())`
   читало событие до того, как планировщик запускал рабочий поток фейка, — утверждение
   исправлено на `running.wait(2)`, тем же приёмом, который файл уже использует в конце
   того же теста. Второй и третий остались: первый полагается на то, что случайная ротация
   при конечном числе попыток никого не обойдёт, второй — на подмене одного символа
   дайджеста (`proxy_workbench/secrets.py` этой сессией не менялся). **Три полных прогона
   подряд после исправления гонки: `Ran 2437 tests … OK (skipped=1)` ×3.** Владельцам двух
   оставшихся тестов стоит убрать зависимость от случайности.
