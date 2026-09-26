# Таблица трассировки: Proxy Workbench, обязательный scope

Ревизия: ветка `integration/ultra-2026-09-25`, HEAD `ccb0954`, версия продукта
`2.3.0` (`proxy_workbench/branding.py:14`).

Документ переписан **после** серии ремонтов `414bd06`, `3e7c953`, `7902612`,
`3fb43d3`, `7a6a526`, `ccb0954`. Все статусы ниже пересчитаны по фактическому
состоянию дерева, а не по отчёту предыдущей сессии.

**Снимок состояния: 26.09.2026, 11:56–12:19, HEAD `ccb0954` плюс
незакоммиченные параллельные правки.** Пока этот документ писался, в рабочем
дереве **одновременно правил кто-то ещё**. На 12:19 `git status` показывал
изменёнными `proxy_workbench/`: `anonymity.py`, `diagnostics.py`,
`exportsvc.py`, `formats.py`, `geo.py`, `geoip.py`, `importer.py`, `jobs.py`,
`pools.py`, `probes.py`, `profiles.py`, `reputation.py`, `scheduler.py`,
`source_adapters.py`, `source_catalog.py`, `source_management.py`,
`sourcedesk.py`, `source-catalog.json`, а также `tests/test_exportsvc_singbox.py`
и 21 новый файл `tests/test_areacatalog_*`, `tests/test_areajobs_*`,
`tests/test_areaexport_*`, `tests/test_areasources_*`, `tests/test_areasecrets_*`,
`tests/test_probearea_*`. Файлы `apikeys.py` и `secrets.py` в 12:01–12:17 тоже
менялись, но к 12:19 в списке изменённых их уже не было — вероятно, чужая
сессия откатила свою правку. HEAD всё это время оставался `ccb0954`.

Два вывода из моих проверок **устарели прямо в ходе работы** и помечены в
тексте как «в полёте»: F20 (12:16) и дефект 14 (12:01). Остальные проверенные
факты относятся к файлам, которые в это окно не менялись
(`proxytool.py`, `db.py`, `gui.py`, `pipeline.py`, `apiv1.py`, `desktop.py`,
`proxy_workbench/ui/app.js`, `packaging/*`) и потому действительны на момент
проверки.

Файлы, которыми я владею, — только эти два документа. Код не менялся: чужие
изменения в коммит не попали.

Источники обязательных ID:

- `docs/requirements/MASTER-PROMPT.ru.md` §3 (дефекты 1–26), §4 (F01–F28), §5 (F29), §7 (22 сквозных сценария);
- `docs/requirements/REVIEW.ru.md` §4 (R01–R20);
- `docs/requirements/audit/AUDIT_AND_ROADMAP.ru.md` (исходные 40 замечаний F01–F40);
- `docs/requirements/audit/BACKLOG.ru.md` (140 задач);
- `docs/requirements/product-research/04-feature-cards.ru.md` (28 карточек);
- `docs/requirements/product-research/03-idea-catalog.ru.md` (X01–X08 и отвергнутые R-идеи).

Чтобы не смешивать два разных пространства имён, ниже аудит finding помечен
`A-FNN`, а карточка исследования — `K-FNN`.

Правила статусов, применённые в этом документе:

| Статус | Что означает |
| --- | --- |
| `verified` | функция реализована, достижима снаружи и подтверждена проверкой, которую можно повторить |
| `implemented_unverified` | написана и связана с пользовательским путём, но конкретное поведение я не подтвердил наблюдением |
| `in_progress` | начато, обязательная часть не сделана |
| `todo` | не начато |
| `external_blocker` | упёрлось во внешнее ограничение, которое я не могу снять |
| `not_applicable_with_evidence` | неприменимо, с доказательством из требований |

Число зелёных тестов статус не поднимает. Тест, который я не запускал и не
воспроизводил, в графе «чем подтверждено» не пишется.

---

## 0. Что проверено в этой сессии

| Проверка | Команда / сценарий | Результат |
| --- | --- | --- |
| Полный набор тестов | `.venv/bin/python -m unittest discover -s tests` | `Ran 2480 tests in 173.529s` / `OK (skipped=1)` / **код выхода 0** |
| Функциональная приёмка снаружи | `.venv/bin/python functional_acceptance.py` | 30 `done`, **код выхода 0** |
| Резервные копии | `python -m proxy_workbench backup` | перечисляет реальные файлы, включая `proxies.sqlite3.v0.pre-migration.20260925T220106.bak` и `proxies.sqlite3.v14.pre-migration.20260926T080721.bak` с манифестами |
| Живой прогон конвейера | локальный mock-прокси на `127.0.0.1`, `run --count-what … --max-requests …` | см. §0.1 |
| Новые маршруты интерфейса | HTTP-запросы к поднятому `gui.make_server` по `/api/api-keys`, `/api/import/*`, `/api/pools*`, `/api/schedules`, `/api/gateway/*`, `/api/maintenance/*`, `/api/sources/*` | см. §0.2 |
| Воспроизведённые дефекты | `/tmp/pw_idem_repro.py`, `diagnose health`, замер `db.columns()` | см. §0.3 |
| Падающий тест на текущем дереве | `.venv/bin/python -m unittest tests.test_probes_reference.CapabilityMatrixTests` | `Ran 2 tests`, `FAILED (failures=3)` — см. §0.4 |

Все сетевые проверки сделаны на loopback: локальный HTTP-прокси и локальная
цель. Публичные прокси, сторонние сервисы и живые каталоги источников не
запрашивались, поэтому **скорость и качество живых публичных прокси по-прежнему
никем не измерены**, и в этом документе нет ни одной цифры о них.

### 0.1 Живой прогон (F12) — локальный mock, четыре кандидата

```
collect --no-sources --input list.txt --allow-private-endpoints   → Unique candidates in the database: 4
run --url http://service.invalid/health --min-success 1 --count-what endpoint
  → Workers: 4; full pass; profile 7e146526bf46d1aa4e26
  → Checked 4/4; 131.2 proxies/s     → matching 1; saved 1
run --count-what exit --want 1
  → Cannot reach the requested number of confirmed exit IPs: this profile has no judge.
    Add --judge-url (it also turns the anonymity check on) or count addresses with --count-what ip.
```

Тот же путь в процессе, с чтением счётчиков `run_state`:

| Что задано | `checked` | `requests` | `stop_reason` | `chain_stop` |
| --- | --- | --- | --- | --- |
| без бюджетов | 3 | 3 | `complete` | `items_exhausted` |
| `--max-requests 1` | 1 | 1 | `E_LIMIT_BUDGET` | `budget_exhausted` |
| `--max-requests 2` | 2 | 2 | `E_LIMIT_BUDGET` | `budget_exhausted` |
| `--want 1 --count-what exit` | 0 | 0 | `want_unreachable_exit` | — |

Три единицы `--count-what` различаются и по-разному объясняют недостижимость:
`endpoint` (адреса), `ip` (уникальные адреса — при четырёх кандидатах на одном
хосте `want=2` даёт `want_unreachable_ip`), `exit` (подтверждённые выходные —
отказ до сканирования, если в профиле нет judge).

**Чего я не измерил:** `--run-max-bytes`. На моей фикстуре тело ответа не
читается (`run_state['bytes'] == 0` при успешном проходе), поэтому бюджет
байтов на глазах не срабатывает и проверить его этим прогоном нельзя. Он
подключён в коде (`proxytool.py:1974-1982`, `chain.Budgets(max_bytes=…)`), но
числом я его не подтверждаю.

### 0.2 Новые маршруты интерфейса — живой HTTP по поднятому серверу

Все ответы ниже — реальные ответы `gui.make_server` на `127.0.0.1`, не чтение
исходника.

| Маршрут | Ответ | Что это доказывает |
| --- | --- | --- |
| `GET /api/api-keys` | 200, список без секретов + 26 прав + журнал операций | менеджер ключей существует в интерфейсе |
| `POST /api/api-keys/bootstrap` | 200, `secret` показан один раз | первый админ выдаётся из интерфейса |
| `POST /api/api-keys` | 200, права ровно заданные (`read.status`, `read.results`) | выпуск ключа с выбором прав и scope |
| `POST /api/api-keys/action` (`rotate`) | 200, новый `secret`, тот же `id` | ротация работает из интерфейса |
| `GET /api/import/preview` | 200, `batch_id`, `mapping`, `columns`, `can_commit` | предпросмотр импорта ничего не пишет и предлагает разбор колонок |
| `POST /api/import/commit` | 200, `rejected: [{line 2, E_IMPORT_PRIVATE}, {line 3, E_IMPORT_MISSING_FIELD}, …]` | отчёт с номерами отклонённых строк |
| `POST /api/import/commit` повторно | 200, `replayed: true` | идемпотентный commit |
| `POST /api/pools/create` | 200, пул с профилем, `desired`, `reserve` | пулы в интерфейсе |
| `GET /api/pools` | 200, пул со `state`, `deficit_reason` | список пулов |
| `POST /api/pools/action` (`refill`) | 200 | наполнение по явному действию |
| `POST /api/schedules/action`, `GET /api/schedules` | 200, интервал 30 мин, `Europe/Berlin`, бюджеты | расписания в интерфейсе |
| `GET /api/gateway/options`, `POST /api/gateway/config` | 200, пулы/профили/поколения | привязка шлюза настраивается |
| `GET /api/maintenance/cleanup-preview` | 200, что удаляется и что остаётся | предпросмотр уборки |
| `GET /api/maintenance/retention-preview` | 200, `runnable: true`, `targets`, `blocked: []` | retention с предпросмотром |
| `POST /api/maintenance/restore-preview` | 200, `source_path`, `target_path`, `files` | предпросмотр восстановления по настоящему бэкапу (источник — полный путь; имя файла без пути даёт понятный `E_DATA_BACKUP_FAILED`) |
| `POST /api/sources/exclude-scope` | 200, `delivered: 3, exclusive: 2, shared: 1, excluded: 2` | общий адрес не отнимается у источника молча |
| `POST /api/sources/exclude-scope` + `include_shared` | 200, `excluded: 1` (третий, разделяемый) | явное согласие на общий адрес |
| `GET /api/sources/scope` | 200, `count: 2`, список нормализованных адресов | список исключений |
| `POST /api/sources/recover` (неизвестный источник) | 200, `known: false` + текст ошибки | честный отказ вместо «ок» без работы |
| `POST /api/sources/scope/clear` | 200, `removed` / `remaining` | снятие исключений |

Четыре маршрута, которые раньше отвечали «ок» без работы
(`gui.py:4063,4069,4071`, `3942`), делают настоящую работу. Исключения
складываются в `data/gui-scope-exclusions.json` (`gui.py:473`) — таблицы
`candidate_scope_exclusion` в `db.py` нет (см. §4).

### 0.3 Дефекты, которые я воспроизвёл, а не только прочитал

| Дефект | Воспроизведение | Факт |
| --- | --- | --- |
| Повтор `POST /v1/keys` с тем же `Idempotency-Key` отдаёт полный секрет дважды | первый ответ `200`, `id=ee69ad8763ccbb92`, `secret=pwk_634cd7c2…`; повтор — тот же `id` и **тот же** секрет; в `api_keys` одна строка | **жив**, правка из `HANDOFF/fix-keys.md` не применена |
| `diagnose health` печатает ложную тревогу | `python -m proxy_workbench diagnose health --data data` → `health: 5 checks, 5 problems`; при этом `health_report(...).failures` = 1 | **жив**, `proxytool.py:4615` фильтрует по `state`, которого нет |
| `db.upsert_endpoint` спрашивает схему на каждый вызов | 5000 вызовов → `columns()` 7 мкс/вызов, весь `upsert_endpoint` 33 мкс/вызов; на 50 000 строк это ≈0.4 с только на `PRAGMA table_info` | **жив**, кэша в `db.py` нет |

### 0.4 Падающий тест, появившийся в 12:16

```
$ .venv/bin/python -m unittest tests.test_probes_reference.CapabilityMatrixTests
Ran 2 tests in 0.000s
FAILED (failures=3)
  CapabilityMatrixTests.test_unmeasured_transports_are_declared_unsupported
    (capability='websocket_handshake')  AssertionError: True is not false
  CapabilityMatrixTests.test_unmeasured_transports_are_declared_unsupported
    (capability='long_lived_connection')  AssertionError: True is not false
  CapabilityMatrixTests.test_unmeasured_transports_are_declared_unsupported
    (capability='media_manifest_segment')  AssertionError: True is not false
```

Тест `tests/test_probes_reference.py:218-224` утверждает, что WebSocket,
длительное соединение и media **не поддерживаются** (`assertFalse(...['supported'])`).
Параллельная правка `probes.py` в 12:16 сделала их `supported: True` и добавила
реализацию. Тест не обновили. Проверено дважды: в полном прогоне с `-v`
(строка 2206 `/tmp/pw_unittest_v.log`) и при запуске модуля отдельно.

**Следствие для версии отчёта.** Прогон `Ran 2480 tests … OK (skipped=1)`,
приведённый выше и снятый мной в 11:58, относится к дереву **до** этих правок
и сейчас уже не воспроизводится: дерево стало больше и содержит падающий тест.
Полного зелёного прогона на текущем дереве я не делал и не заявляю.

---

## 1. F01–F29 — функциональные направления

| ID | Что означает | Статус | Чем подтверждено |
| --- | --- | --- | --- |
| F01 | Явные режимы проверки, basic без URL, честное различие отказа цели и отказа прокси | `implemented_unverified` | Код: `probes.py:161` `COLLECT_ONLY`, `:178` карта режимов, `capability_matrix()` `:2223`. Живой прогон §0.1 шёл с явным `--url`. Не выполнено: сценарий «найти 5 рабочих без URL» по живой сети — внешняя сеть запрещена условиями |
| F02 | Коллекции, membership, явный scope, изоляция от публичной базы | `verified` | `db.py` collections+membership, `proxytool.add_members`. Сквозной сценарий §7.5: `tests/test_acceptance.py::test_a_personal_import_after_a_big_public_sweep_checks_only_that_collection`; дефект 11 на реальной legacy-базе — `tests/test_db_collections.py`. Импорт в свою коллекцию через интерфейс проверен: `POST /api/import/commit` → `collection_id: public-base`, `members` |
| F03 | Импорт: файл/буфер, форматы, preview, merge/replace, отмена, идемпотентный commit | `verified` | Живой HTTP §0.2: `POST /api/import/preview` → 200 с `batch_id`/разбором колонок, `POST /api/import/commit` → 200 с номерами отклонённых строк и кодами, повтор → `replayed: true`. CLI `import preview\|commit\|list` тоже работает. **Ограничение, которое я не проверял:** drag-and-drop в браузере (драйвера нет), формат `json`, режим `replace` — код написан, но эти три варианта я не гонял |
| F04 | HTTP Basic / SOCKS5 auth, hostname, отдельные access identity, OS vault | `in_progress` | **Механизм есть**: `secrets.py` (`AccessStore`, `rotate` поднимает `access_revision`). Перепривязка теперь тоже поднимает ревизию: `tests/test_db_secrets.py::test_rebind_moves_the_reference_and_advances_the_revision` ждёт `access_revision == 2`, `tests/test_secrets_access.py::test_a_rebind_to_a_matching_revision_keeps_working` готовит замену на новой ревизии (коммит `ccb0954`). **Транспорт аутентификации не реализован**: `probes.py:589`, `proxytool.py:138,329,1107,1123` отвергают любой `username`/`password` в URL; grep по `Proxy-Authorization\|proxy_auth\|ProxyBasicAuth` даёт 0 совпадений. macOS `security` CLI сознательно не сделан (пароль в argv), Windows OS-vault — нет |
| F05 | Именованные профили, версии, all/any/K, required/optional, fail-fast | `implemented_unverified` | Код `profiles.py`; `profiles_module.run_request` вызывается из `api.py:2212`. Пресеты и `ServiceSet` в интерфейсе. **Не сделано: переключение профиля в CLI/GUI не исполняет профиль** — `proxytool.check_proxy`/`allowed_failures` по-прежнему считают допуск по одному глобальному `min_success` |
| F06 | Каталог сервисов и наборов с точными probes и датой проверки определения | `implemented_unverified` | Код `servicecatalog.py`; `Workbench.presets` → `search_presets`. В UI 2 вхождения `service.?set`. Границы честно записаны: Steamworks API-хост официальной документацией **не подтверждён**, Reddit Data API вернул 403, поэтому это homepage-пробы с раскрытием в `not_proved` |
| F07 | Расширенные параметры, единицы, лимиты, единая валидация, никакого молчаливого игнора | `implemented_unverified` | Код `probes.py` (валидация с лимитами), `E_VALIDATION_UNKNOWN_FIELD` в `pools.py:246`, `apikeys.py:89`, `scheduler.py:155`. **Остался тот же контрпример**: `allow_private` объявлен в теле `POST /v1/collections` и `PATCH` и попадает в `openapi.json`, но `grep -c "allow_private" proxy_workbench/api.py` = **0**, а в таблице `collections` такой колонки нет: параметр принимается с кодом 200 и молча выбрасывается. Проверено на текущей ревизии, не изменилось |
| F08 | Страны: include/exclude, endpoint vs exit, unknown policy, GeoIP, ASN | `in_progress` | Модуль `geo.py` полон. `proxytool.py:2615` `country_criterion` и `:2658` `geo_country_verdict` вызываются внутренне, но `Workbench.country_criterion` (`:3099`) и `Workbench.filter_by_country` (`:3112`) не имеют ни одного вызывающего: `grep -rn "filter_by_country" proxy_workbench/*.py` даёт только определение. `api.py` и `gui.py` фильтруют страну своей строкой. **Схема не держит модель**: в `db.py` нет ни `endpoints.hosting_basis`, ни `observations.exit_ip`/`exit_country`, ни `pools.quota_basis` (`grep -n "hosting_basis\|exit_ip\|exit_country" proxy_workbench/db.py` — 0 совпадений) |
| F09 | Freshness и доказательства: единый admission, checked_at/published_at, fail не маскируется старым success | `implemented_unverified` | `core.py` (`admit`, `select`, `POLICY`); `api.py:270-315` `Exports.load` отдаёт только свежие строки. Сквозные тесты: `test_an_unknown_measurement_time_is_not_fresh`, `test_a_re_export_cannot_move_a_measurement_time`, `test_one_expired_row_does_not_empty_the_set`. **Ограничение**: сквозной путь до нового соединения я не гонял |
| F10 | Диагностика стадий, воронка, восстановительное действие | `implemented_unverified` | CLI `diagnose funnel\|zero\|control\|health\|bundle` (`proxytool.py:4571`) работает; `diagnostics.py` (`build_funnel`, `explain_zero`, `check_control`). **Не сделано в GUI**: `grep -c "funnel" proxy_workbench/ui/app.js` = **0**, `grep -c "funnel\|diagnose" proxy_workbench/ui/index.html` = **0**. Живой сетевой путь `check_control` я не выполнял. **Дефект воспроизведён** (§0.3): `diagnose health` печатает `5 checks, 5 problems` на исправной папке |
| F11 | Задания: персистентные состояния, очередь, pause/resume/cancel, crash recovery, идемпотентность | `verified` | `jobs.py` + `proxytool.py:2022-2065`, `Workbench.jobs()`. Живой прогон §0.1 выдал `job-…` и отработал без потери результата. `POST /v1/checks/check` вернул **HTTP 202** в функциональной приёмке. `test_acceptance.py::test_one_idempotency_key_does_not_start_a_second_run`; тесты `tests/test_jobs_*` зелёные |
| F12 | Конвейер: стадии, первые результаты, find-N, backpressure, ресурсные бюджеты | `implemented_unverified` (было `in_progress`) | **Конвейер теперь ведёт прогон.** `proxytool.py:2231-2252`: `chain.PipelineConfig(sources=(chain.SourceSpec(..., fetch=candidates),), budgets, limits, find, initial_met=seeded, runners=chain.Runners(cheap, basic, expensive), run_cheap/run_basic/run_expensive)`, `chain.Pipeline(..., concurrency=chain.AdaptiveConcurrency(minimum=1, maximum=budgets.worker_ceiling(), target_success=0.8, window=16, increase=2, decrease_factor=0.5))`. Пер-хоста — `chain.Limits(max_per_host, min_host_interval_s)` (`:1994`), judge — `chain.TargetPolicy(EXPENSIVE_TARGET, max_inflight, max_requests)` (`:1992`). Живой прогон §0.1: `--max-requests 1` → `checked: 1, requests: 1, stop_reason: E_LIMIT_BUDGET, chain_stop: budget_exhausted`; три единицы `--count-what` различаются и объясняют недостижимость. `--workers` — потолок (`Budgets(max_inflight=workers)`, `worker_ceiling()`), а не число воркеров. История выбирается детерминированно: `proxytool.py:1612` `newest_measurements` сравнивает `_recency`, а не порядок строк. **Что осталось**: (1) `EXPENSIVE_UNTIL_N` (`pipeline.py:113`) в продукте не используется — выбран `EXPENSIVE_ALL_PASSING` (`proxytool.py:2246`); (2) поток «источник → проба» есть **внутри** скана (`candidates()` читает коллекцию постранично), но не между `collect` и проверкой — docstring `proxytool.py:2063-2071` говорит об этом прямо: «`collect` still fills the collection first»; (3) `--run-max-bytes` я числом не подтвердил (§0.1) |
| F13 | Полный source manager: каталог, адаптеры, 304, last-good, карантин | `implemented_unverified` | `sourcedesk.py`, `Workbench.sources()`, `collect`, `import_clash`/`import_singbox`. **Четыре маршрута, которые раньше отвечали «ок» без работы, теперь делают работу** — проверено живым HTTP (§0.2): `exclude-scope` отделил 2 эксклюзивных адреса от 1 разделяемого, `scope` вернул список, `recover` на неизвестном источнике дал честный отказ, `clear` снял исключения. **Осталось**: `REQUESTED_DDL` (`sourcedesk.py:1371-1414`, `source_feed`/`membership_source`) объявлен константой и «this module never executes it» — в `db.py` такой миграции нет, поэтому `SourceDesk` работает против схемы, которая в живой базе не создаётся; ни одного живого `fetch`, 304 или last-good я не наблюдал |
| F14 | Постоянный пул: desired N, резерв, refill, cooldown, квоты | `implemented_unverified` | `pools.py`; `Workbench.pool_refill` вызывается из `api.py`; в интерфейсе `POST /api/pools/create` → 200, `GET /api/pools` → 200 со `state`/`deficit_reason`, `POST /api/pools/action refill` → 200 (живой HTTP, §0.2). **Не сделано: периодический watch** — `grep -rn "pools.watch" proxy_workbench/` даёт 0 вызывающих, пул сам до N не восстанавливается. Квоты по exit-IP нечем хранить: в таблице `pools` нет `quota_basis` |
| F15 | Расписания, timezone/DST, quiet hours, бюджеты, уведомления | `implemented_unverified` | `scheduler.py`; в интерфейсе `POST /api/schedules/action` → 200, `GET /api/schedules` → 200 с интервалом, `timezone`, окнами и бюджетами (§0.2). **`mark_wake()` теперь вызывается продуктом**: `desktop.py:1944` `wake_tick` → `engine.mark_wake()`, `desktop.py:1909` `store.mark_awake(job.id)`; прежняя строка «не имеет ни одного продуктового вызова» больше не верна. **Что осталось**: таблица `schedules` (`db.py:775-777`) по-прежнему несёт 11 колонок и не содержит `last_run_at`, `paused`, `pause_reason`, `dst_policy`, `catch_up`, `wake_gap_s` — интервальная сетка и накопленный бюджет не переживают перезапуск процесса. `SqliteScheduleStore.persists_runtime_state` остаётся `False` |
| F16 | Gateway: binding к пулу+профилю, ротация, sticky, лимиты, transport parity | `implemented_unverified` | `gateway.py:604-637` `reserve()` — слот берётся **до** первого `await`; `Binding.pool_id`; `denylist`; session TTL. В интерфейсе `GET /api/gateway/options` и `POST /api/gateway/config` отвечают 200 и сохраняют `pool_id`/`profile_id`/`profile_revision` (живой HTTP, §0.2). **Что осталось**: (1) `gui.py:4226` и `gui.py:1141` создают `gateway.Background(data, host, port, token=…)` **без** `bind=self.gateway_binding()` — выбранный пул сохраняется, валидируется и вызывает перезапуск слушателя, но сам объект `Binding` в слушатель не передаётся, то есть работающий шлюз обслуживает привязку по умолчанию; (2) `--lan` объявлен (`gui.py:4165`) и печатает предупреждение (`:4221`), но `lan=True` не передаётся в `gateway.Background`, а `Bind.__post_init__` (`gateway.py:178`) отвергает не-loopback адрес без `lan=True` — включить LAN из интерфейса по-прежнему нельзя; (3) `--gateway-interface` отсутствует; (4) у CLI-команды `gateway` нет `--pool` |
| F17 | Законченный путь подключения: пул → приложение → поля → контроль маршрута → отключение | `implemented_unverified` | Страница шлюза, recipes, QR `app.js:4068` `makeQR`. **Не сделано**: контроль маршрута (last_proxy/pin) в снимке пула отсутствует; **round-trip декодирование QR не проверяется** — `tests/test_qr.py` (29 строк) ищет в исходнике `upward = !upward` и наличие `class="qr-svg"`, но не декодирует QR обратно в исходный URL (R20 отмечал это три цикла подряд) |
| F18 | Единое управление GUI/CLI/API, одинаковые units/codes/permissions | `implemented_unverified` | `api.py` вызывает `profiles.run_request`, `pools`, `jobs`, `scheduler`, `sourcedesk`; новые маршруты интерфейса вызывают те же `workbench.pools()`, `Scheduler`, `importer`, `apikeys`, что и API (§0.2). `tests/test_profiles_parity.py` зелёный. **Не сделано**: `tests/test_parity` не существует; равенство выдачи GUI/API/CLI на одном scope доказано только для профилей |
| F19 | Работа со списком: page/selected/all-matching, bulk, теги, saved views, выделенное не переносится на другой scope | `implemented_unverified` | 26 вхождений `bulk` в `app.js`; `proxytool.py:2758` расчёт `kind` (`selection` / `top` / `published`) и `:2931` отдельная ветка — экспорт выделенного не публикует поколение. Три зелёных теста на это. **Прошла часть, которую раньше считали не начатой**: теги (`gui.py:2493-2514`, `tags_in_use`), избранное и заметки (`gui.py:2191,2345-2346`, сайдкар `gui-annotations.json`), **сохранённые представления** (`gui.py:2599-2629` `saved_views`/`store_view`/`delete_view`, маршруты `GET/POST /api/views`, `/api/views/delete`), **матрица proxy × target** (`gui.py:2260` `matrix`, маршрут `GET /api/results/matrix`). **Не проверено мной**: ни один из этих маршрутов я не вызывал; сравнение запусков (`RES13`) по-прежнему не сделано |
| F20 | Дополнительные измерения: WebSocket, длительность, media, self-hosted reference probe | `in_progress` (было `implemented_unverified`) — **статус менялся в ходе этой сессии** | **Менялось на глазах.** В 11:56 `capability_matrix()` объявлял `websocket_handshake`, `long_lived_connection` и `media_manifest_segment` как `supported: False`, и весь предыдущий отчёт на этом строилось. В 12:16 `probes.py` переписан параллельной правкой: `run_websocket:2719`, `run_duration`, `run_media`, `summarize_websocket:2628`, `parse_media_manifest:2690`, лимиты `WEBSOCKET_LIMITS`/`DURATION_LIMITS`/`MEDIA_LIMITS` (`:2438-2446`), состояния `ok | not_upgraded | no_pong | closed | error`, и эндпоинты reference probe `/ws`, `/hold`, `/manifest.m3u8`, `/segment/1` (`:3120-3126`) плюс негативные `/manifest-empty.m3u8`, `/manifest-missing.m3u8`, `/manifest-bad-segment.m3u8` (`:2994-2996`). Проверено вживую в 12:17: `probes.capability_matrix()` отдаёт `websocket_handshake True`, `long_lived_connection True`, `media_manifest_segment True`, `udp_transport False`, `http2_or_http3 False`, `calls_video_any_service False`. **Почему не `verified`**: (1) результатов этих проб я не получал — ни одного живого прогона WS/hold/media не было; (2) **появился падающий тест**: `tests/test_probes_reference.py:218` `test_unmeasured_transports_are_declared_unsupported` всё ещё требует `supported == False` для трёх этих возможностей и падает в трёх подтестах (проверено в 12:14 и в 12:17). Это не «зелёное дерево» — это рассинхрон кода и теста, возникший минуту назад. Reference probe (`reference_probe_targets`, `serve_reference_probe`) существовал и раньше |
| F21 | Сравнение источников и провайдеров: cohorts, survival, overlap, стоимость пригодного адреса | `implemented_unverified` (было `todo`) | **Функция написана**: `sourcedesk.py:2468` `compare_sources`, `:2808` `compare_suppliers`, `:2846` `compare_cohorts`, `:2719` `survival_across_windows`, `:2447` `_overlaps`, `:2647` `_cohort_warnings`; тесты `tests/test_sourcedesk_compare.py` (43 теста) зелёные. **Не сделано: не подключено ни к одной пользовательской поверхности** — `grep -rn "compare_cohorts\|compare_sources\|compare_suppliers\|survival_across_windows" proxy_workbench/gui.py proxy_workbench/api.py proxy_workbench/apiv1.py proxy_workbench/proxytool.py` даёт 0 совпадений; в CLI нет ни одной такой команды. Функция жива, но снаружи недостижима, поэтому `todo` снят, а `verified` не поставлен |
| F22 | Удобная фоновая работа: меню-бар, close vs quit, повторный запуск, autostart, sleep/wake | `implemented_unverified` (было `todo`) | **Слой написан**: `desktop.py` — `TRAY_HELPER_SOURCE:1963` (≈250 строк Swift, `NSStatusItem`), `ensure_tray_helper:2339` (компилируется `swiftc` один раз в `<cache>/tray/`, под именем с хешем исходника), `InstanceLock`/`instance_lock_path:1291`, `ControlServer`/`control_request`, `autostart_status:1606`/`enable_autostart:1672` (LaunchAgent / `HKCU\…\Run` / XDG), `PlatformObserver`/`network_fingerprint`/`wake_tick:3131`/`run_startup_migration`. `mark_wake` и `mark_awake` теперь вызываются продуктом (`desktop.py:1944`, `:1909`). Владелец отчитался о 48 проверках из 48 на macOS через `ps`, локальный сокет и файлы (`HANDOFF/fix-desktop.md`) — **это его отчёт, я его не перепроверял**. **Что осталось**: (1) `packaging/build_macos.py` и ни один из четырёх `.spec` не кладут helper в bundle, поэтому `ensure_tray_helper` для frozen-сборки возвращает `(None, «в сборке нет helper меню-бара»)` и **в собранном `.app` меню-бара нет**; (2) Windows и Linux не проверялись ни разу — ни `winreg`, ни loopback-канал, ни XDG autostart не запускались; (3) `python -m proxy_workbench` без аргументов идёт в `gui.main` (`__main__.py:9-10`), а не в `desktop.main`, поэтому меню-бар есть только у frozen-сборки (через `packaging/launcher.py`) и при `python -m proxy_workbench.desktop`; (4) `proxy_workbench/ui/app.js` фоновые события не показывает: `grep -n "desktop-journal\|/api/desktop" proxy_workbench/ui/app.js` — 0 совпадений, и маршрут `GET /api/desktop`, предложенный владельцем в handoff, в `gui.py` не добавлен |
| F23 | Поставка Windows/macOS/Linux, per-user пути, обновления, подпись | `external_blocker` | Сборщики и spec присутствуют (`packaging/build_macos.py`, `build_windows.py`, 4 spec, `windows-installer.iss`, `verify_release.py`). **Артефакт macOS отстаёт**: `/tmp/pw-dist/Proxy Workbench.app` + `.dmg` + `.zip` + manifest — версии **2.2.1 от 25.09 21:49** при текущей `2.3.0`; пересборка и проверка на текущей ревизии не выполнены. **Внешний блокер**: `PyInstaller` в `.venv` отсутствует (`.venv/bin/python -c "import PyInstaller"` → `ModuleNotFoundError`, проверено в этой сессии), установка требует сети; подписанных артефактов нет, манифест отдаёт `"signed": false` (`packaging/release_manifest.py:3-5` — флаг ставится только после реальной проверки подписи). Windows-сборка ни разу не выполнялась (машина macOS arm64, `iscc` не установлен) |
| F24 | Данные, backup, restore, retention/cleanup с preview | `verified` | CLI: `proxytool.py:4375` `_cmd_backup`, `proxytool.py:4501` `_cmd_backup_rebind` — `backup create\|list\|verify\|preview\|restore\|rollback\|retention\|cleanup\|rebind`, каждое изменяющее действие с `--apply`. **Миграции 1..12 починены** (`db.py:1148` регистрирует `endpoint_id` на каждом соединении, а не только в `_m0`), retention preview и apply считают один план (`_retention_targets`), `rebind_secrets` поднимает `access_revision`/`rotated_at` и печатает `revisions` (`db.py:432-441`). **Живое подтверждение в этой сессии**: `python -m proxy_workbench backup` перечислил `proxies.sqlite3.v0.pre-migration.20260925T220106.bak` и `proxies.sqlite3.v14.pre-migration.20260926T080721.bak` с манифестами — то есть снятие копии перед неаддитивной миграцией работает на промежуточных версиях, а не только на legacy `v0`. **В интерфейсе**: `GET /api/maintenance/cleanup-preview` → 200, `GET /api/maintenance/retention-preview` → 200 с `runnable`/`targets`/`blocked`, `POST /api/maintenance/restore-preview|restore` (живой HTTP, §0.2) |
| F25 | Помощь и диагностика: коды, bundle с preview/redaction, версии/scope/job health, fixture recipe | `implemented_unverified` | `diagnose health\|bundle\|zero\|funnel\|control` в CLI работает; `diagnostics.build_bundle` пишет `diagnostics/bundle.json` с числом redactions. **Не сделано в GUI**: ни кнопки диагностического пакета, ни счётчиков воронки, ни объяснения «почему 0 результатов» (`grep -c funnel app.js` = 0). **Дефект воспроизведён**: `diagnose health` на исправной папке печатает `5 checks, 5 problems` — `HealthCheck.to_dict()` (`diagnostics.py:1596-1598`) отдаёт поле `ok`, а `proxytool.py:4615` считает проблемами `item.get('state') != 'ok'`, поля `state` в словаре нет |
| F26 | OSS, шаблоны, доступность, RU/EN, документация под реальные возможности | `implemented_unverified` | `CHANGELOG.md` `[Unreleased]` заполнен, версия 2.3.0, README.md/README.ru.md по совпадениям `api-key\|/v1/`. **Переводы отстают**: встроенные `messages.en` и `messages.ru` содержат 1143 и 1217 ключей, а каждый из десяти пакетов `proxy_workbench/ui/i18n/*.js` — ровно 791 (784 уникальных), то есть свыше 350 новых ключей (менеджер ключей, импортёр, пулы и расписания, хранение, фоновый слой) в них отсутствуют и `t()` (`app.js:2455-2462`) отдаёт для них английский текст. **Не сделано**: `grep -c "api-key\|/v1/" SECURITY.md` и `CONTRIBUTING.md` = 0; README описывает `--workers` как «128 workers» и «Raise `--workers`», хотя это теперь потолок, и не упоминает ни `--max-requests`, ни `--run-max-bytes`, ни `--count-what`, ни команду `bench`; `tests/test_qr.py` проверяет исходный текст, а не поведение (запрещено MASTER-PROMPT §7) |
| F27 | URL-подписки и жизненный цикл feed | `implemented_unverified` | `sourcedesk.py` `import_subscription`/`import_clash`/`import_singbox`/`feed_diagnostics`, ETag/Last-Modified/304, last-good, redaction секретных URL. **Не сделано, и это по-прежнему верно**: `REQUESTED_DDL` (`source_feed`, `membership_source`, `sourcedesk.py:1371-1414`) объявлен константой с комментарием «the schema belongs to db.migrate() … this module never executes it», а в `db.py` (`grep -n "source_feed\|membership_source" proxy_workbench/db.py` — 0 совпадений) такой миграции нет: `SourceDesk` работает против схемы из константы, а не против живой БД. Ни одного живого `fetch` я не выполнял |
| F28 | Экспорты и согласованные snapshots | `implemented_unverified` | `exportsvc.py`; общий snapshot contract; `kind='selection'` не публикует поколение; `api.py:270-315` фиксирует поколение один раз и сверяет `_manifest_digest`. **Не сделано, подтверждено в этой сессии**: `ExportOptions.client_target`/`client_binary` не заполняются ниоткуда — сигнатура `proxytool.py:2682` объявляет их со значением `None`, единственное место, где они передаются дальше, — `proxytool.py:2880`, а все вызовы с непустым значением находятся только в `tests/test_exportsvc_*.py`. Поэтому `singbox_target(None)` всегда даёт `unconfigured/legacy`, и `client_check()` с настоящим бинарником sing-box недостижим. Независимая находка: `exportsvc.py:1380-1403` при `FileExistsError` вызывает `remove_generation()` и может стереть чужое поколение |
| F29 | Полноценное API и менеджер ключей | `implemented_unverified` (было `in_progress`) | **Менеджер ключей в интерфейсе теперь есть и проверен живым HTTP** (§0.2): `GET /api/api-keys` без секретов, `POST /api/api-keys/bootstrap`, `POST /api/api-keys` с выбором прав, `POST /api/api-keys/action` (`rotate`/`update`/`disable`/`enable`/`revoke`/`delete`), страница `#page-keys` (`index.html:1885`). Прежний вердикт «в `gui.py` 0 маршрутов `/api/key*`» больше не верен. **Осталось четыре вещи**: (1) **воспроизведённый дефект**: повтор `POST /v1/keys` с тем же `Idempotency-Key` отдаёт **тот же полный секрет** — `apiv1.py:2044` по-прежнему `self.idempotency.put(bucket, idem_key, digest, response)`, метода `_cacheable` в файле нет, а готовый механизм `apikeys.carries_one_shot`/`without_one_shot` (`apikeys.py:559,573`) не вызывается никем; правка описана в `HANDOFF/fix-keys.md` и в дерево не попала; (2) **per-key `concurrency`** — `Principal.concurrency` записывается (`apiv1.py:546`) и больше нигде не читается, `QuotaGuard` (`apikeys.py:1494`) не имеет продуктового вызова, единственный `ConcurrencyLimiter` (`apiv1.py:711`) пер-серверный; (3) **резервирование** — см. API05; (4) нет read-only subscription secret для клиентов без заголовков |

---

## 2. Дефекты 1–26 (MASTER-PROMPT §3)

| ID | Что означает | Статус | Чем подтверждено |
| --- | --- | --- | --- |
| 1 | Срок годности проходит scanner → БД → GUI/API/export/gateway | `verified` | `core.apply_measurement` пишет `valid_until`; `test_a_re_export_cannot_move_a_measurement_time`, `test_an_unknown_measurement_time_is_not_fresh` (зелёные в прогоне `Ran 2480 tests`). Строка без записанного срока читается как `freshness=unknown` |
| 2 | Переэкспорт и смена watch не создают новое время измерения | `verified` | То же `test_a_re_export_cannot_move_a_measurement_time`; `Exports.load` берёт время из строки, не из `mtime`/`size` публикации |
| 3 | Истечение одного участника не скрывает остальных свежих | `verified` | `api.py:270-315`: `admitted = [row for row in rows if row['freshness'] == 'fresh']`; `test_one_expired_row_does_not_empty_the_set` |
| 4 | Неизвестное время / future / clock rollback дают явное состояние | `verified` | `core.py` (`TIME_UNKNOWN`, `TIME_FUTURE`, `CLOCK_ROLLBACK`), `api.py:203-210` `TIME_STATE_CODES`; `test_an_unknown_measurement_time_is_not_fresh` |
| 5 | Рейтинг, детали и скачивание доступны во время scan/watch | `implemented_unverified` | `gui.py:245-246` класс `Snapshot` читает опубликованное поколение без writer-lock, `gui.py:557-558` `read_db` — тоже, `data_lock` остался только на записи `start`. `tests/test_apiv1_limits.py` зелёные. **Не выполнено мной**: сценарий «таблица и скачивание отвечают во время активного scan» через GUI не запускался, и ни один тест не инстанцирует `Snapshot` напрямую |
| 6 | Recheck/cancel/crash не уничтожает историю и очередь | `verified` | `jobs.py` (персистентные item-состояния, checkpoints, crash recovery); `tests/test_jobs_lifecycle.py`, `test_jobs_recovery.py`. Живой прогон §0.1 отработал с присвоенным job ID |
| 7 | Экспорт выделенного/top-N — отдельный артефакт, активный пул не двигается | `verified` | `proxytool.py:2758` расчёт `kind` (`selection` / `top` / `published`), `:2931` ветка `selection`; `test_exporting_selected_rows_does_not_switch_the_active_pool`, `test_a_top_n_slice_is_its_own_artifact`, `test_a_selection_export_over_the_api_does_not_move_the_active_pool` |
| 8 | Scope/profile/generation фиксированы у потребителей | `implemented_unverified` | `Exports` сверяет `schema_version` и manifest, поколение фиксируется один раз. Ограничение: полный «проверка B не перенаправляет подключение A» на живом соединении я не гонял |
| 9 | Повреждённый указатель даёт одинаковое явное unavailable | `verified` | `api.py:323-327` `_refuse(..., 'E_STATE_NO_SNAPSHOT')`; `test_a_broken_pointer_is_the_same_explicit_unavailable_everywhere` |
| 10 | Hostname/private/auth — сквозной путь либо сразу отказ | `verified` | `importer.EndpointPolicy`; `test_a_hostname_of_your_own_passes_the_whole_path`; `test_a_credentialed_endpoint_is_never_put_in_a_public_artifact`. Живой HTTP §0.2 это же показал на реальном ответе: строки `198.51.100.7` в публичную коллекцию пришли с `E_IMPORT_PRIVATE` |
| 11 | Собственный список изолирован от старой публичной базы | `verified` | `db.py` collections/membership как разные сущности; `test_a_personal_import_after_a_big_public_sweep_checks_only_that_collection` + тест на реальной legacy-базе |
| 12 | Watch/refill восстанавливает пул, а не исключает навсегда | `implemented_unverified` | `pools.py` `refill`/`refill_all` + `tests/test_pools_lifecycle.py`; интерфейс: `POST /api/pools/action refill` → 200 (живой HTTP). **Не сделано снаружи**: `grep -rn "pools.watch" proxy_workbench/` → 0 вызывающих; периодического watch в движке нет |
| 13 | Валидация judge до elite; invalid/empty/CAPTCHA → unknown | `verified` | `probes.py:2248+` validate-before-elite; `tests/test_anonymity.py`. **Оговорка**: тест `tests/test_anonymity.py:206-210`, закрепляющий тихий сброс `min_anonymity` в `any`, остался в дереве и не переписан |
| 14 | DNSBL: IPv6 reverse, зонные коды, quota≠listed | `in_progress` (было `implemented_unverified`) — **статус менялся в ходе этой сессии** | `probes.py` закрыл код: dotted nibbles, `DNSBL_QUOTA`/`DNSBL_ACCESS`/`DNSBL_LISTED`/`DNSBL_CLEAR`. **В 11:56** старая реализация жила параллельно: `reputation.py:184` разворачивал `address.exploded` с двоеточиями вместо точечных нибблов, `reputation.py:237` считал листингом любой `127.*`. **В 12:01 `reputation.py` переписан параллельной правкой** (+194 строки): `reverse_ip` теперь делегирует `probes.reverse_ip`, зонные коды приходят из `probes.DNSBL_*`, а `_dnsbl_error_code` отличает квоту или отказ в доступе от NXDOMAIN. **Проверено в 12:17**: `grep -n 'address.exploded\|startswith("127.")' proxy_workbench/reputation.py` — 0 совпадений, то есть прежние два дефекта в текущем дереве устранены. **Почему не `verified`**: живой DNSBL-запрос я не выполнял (внешняя сеть запрещена), и правка ещё не закоммичена |
| 15 | Скорость: верное окно, минимальный sample, insufficient, отдельные transfer/TTFB | `verified` | `probes.py:113` `INSUFFICIENT_SAMPLE`, отдельные замеры transfer и TTFB; `tests/test_probes_*` |
| 16 | Gateway резервирует слот до await; cancel/error освобождает | `verified` | `gateway.py:604-637` `reserve()`/`_pick(reserve=True)`; `tests/test_gateway_reservation.py`, `test_per_proxy_limit` |
| 17 | HTTP upstream health ≠ TCP-open; нет повторов неидемпотентных | `implemented_unverified` | `gateway.py` различает «туннель установлен» / «получен ответ» / «запрос успешен»; `tests/test_gateway_health.py` |
| 18 | Gateway/GUI/API имеют разные secrets; LAN — явный opt-in | `implemented_unverified` | **Секреты разведены**: `proxytool.py:3517` отдельный `--gateway-token`/`PROXY_WORKBENCH_GATEWAY_TOKEN`, `:3720-3724` отказ при совпадении с API-токеном, `compose.yml` использует обе переменные, GUI генерирует отдельный токен на запуск. **LAN из GUI по-прежнему не включается**: `gui.py:4165` объявляет `--lan`, `gui.py:4221` печатает предупреждение, но `lan=True` не передаётся в `gateway.Background` (`gui.py:4226`), а `Bind.__post_init__` (`gateway.py:178-182`) без `lan=True` отвергает не-loopback адрес — слушатель просто не стартует |
| 19 | Denylist применяется до prefilter и отзывает новые admissions | `implemented_unverified` | `Denylist.match()` до любой сетевой стадии, отзыв admissions; `tests/test_gateway_denylist.py`. Судьба уже открытых streams — отдельная политика |
| 20 | Empty pool никогда не включает DIRECT; конфиги валидируются клиентом | `implemented_unverified` | **fail-closed закрыт**: `formats.py:39,62,72`; `test_an_empty_export_never_selects_a_direct_route`. **Вторая половина открыта**: `client_target`/`client_binary` не заполняются ниоткуда (§F28), `client_check()` покрыт фейковыми исполняемыми файлами (exit 0 / exit 1) |
| 21 | Browser download: picker в активации, одно закрытие, cancel без ошибки, fallback | `implemented_unverified` | `app.js:6082` `downloadFile` — активация до первого `await`, `preventClose: true`, ровно один `close()`/`abort()`, `AbortError` без тоста, fallback через anchor. **Ограничение**: настоящим кликом в Chromium не воспроизведено — драйвера браузера в окружении нет |
| 22 | Источники: путь update, понятные имена, metadata после collect, карантин | `in_progress` (было `todo`) | **Сделано и проверено**: четыре маршрута источников делают настоящую работу (§0.2), `exclude-scope` отделяет разделяемые адреса от эксклюзивных, `recover` честно отказывает на неизвестном источнике; `public_source` (`gui.py:143-153`) больше не стирает идентичность источника — к подписи добавлен короткий устойчивый `core.source_key(value)[:8]`, и два зеркала одного хота различимы; `source_quality` (`proxytool.py:2780-2790`) считается по `result_allowed(..., denylist=None, strict=False, min_anonymity='any')` — то есть **независимо от фильтров текущего экспорта**, и это прямо сказано в комментарии; `prune_sources` (`gui.py:1160-1173`) убирает источник только из настроек, необратимого удаления данных в нём нет. **Не сделано**: `branding.py:18` `SOURCES_URL` по-прежнему указывает на `main/sources.json`; `country_resolver` (`proxytool.py:2507-2516`) снимает снимок `candidate_meta` один раз при старте `main` (`:4830`), поэтому metadata, собранная этим же прогоном, в разрешении стран не участвует; карантин/last-good есть в `sourcedesk`, но экрана карантина в интерфейсе нет; ни одного живого `update`/`fetch` я не выполнял |
| 23 | Quick test подписан объёмом; full recheck отдельно | `implemented_unverified` | `/v1` quick-test требует `budget`/`timeout` в теле и отвечает 202; `tests/test_api.py`. **Не сделано в GUI**: `App.start({'action':'quick'})` без полного подписанного бюджета |
| 24 | Presets не оставляют параметры прошлого сценария; website ≠ весь сервис | `implemented_unverified` | `servicecatalog.py` versioned presets с явным составом и сбросом полей; `capability_matrix()` различает homepage/API/WebSocket/media; `not_proved` и `limitations_ru` раскрыты. **Не сделано**: место показа diff обновления пользователю |
| 25 | Live feed использует реальные события измерений | `in_progress` | Сторона `jobs.py` закрыта: реальные события пишутся в `job_event` по одному на item. **Не сделано**: живая лента в UI по-прежнему строится из хвоста `data/gui-events.jsonl`; `JobStore._emit` из `store()` в `proxytool.py` не вызывается |
| 26 | Реальная Mac-поставка, writable paths, Windows lifecycle, достоверные release notes | `external_blocker` | **Mac-поставка материально существует** (`/tmp/pw-dist/…app/.dmg/.zip` + manifest), но версии 2.2.1 от 25.09 при текущей 2.3.0. **`PyInstaller` в `.venv` нет** (проверено в этой сессии: `ModuleNotFoundError`) → пересборка невозможна без сети. **Windows-сборка ни разу не выполнялась** (нет Windows, `iscc` не установлен). **Подпись — внешний блокер**: Developer ID и учётных данных нотаризации нет, манифест `"signed": false`. **Дополнительно к этому**: даже пересобранный `.app` не получит меню-бара, пока `build_macos.py` и spec не положат helper в bundle (§F22) |

---

## 3. R01–R20 (REVIEW.ru.md §4)

| ID | Статус | Чем подтверждено |
| --- | --- | --- |
| R01 Freshness не проходит весь путь | `verified` | Дефект 1 выше; `test_a_re_export_cannot_move_a_measurement_time` |
| R02 Первый истёкший адрес опустошает snapshot | `verified` | Дефект 3; `test_one_expired_row_does_not_empty_the_set` |
| R03 Таблица и скачивание заблокированы во время scan | `implemented_unverified` | Дефект 5: `gui.py:245-246` и `:557-558` сняли lock с read-пути; сам сценарий «отвечает во время scan» я не запускал |
| R04 Экспорт выделенного меняет общий пул | `verified` | Дефект 7; три отдельных теста |
| R05 GUI может обойти отказ snapshot-reader | `verified` | `api.py:323-327`; `test_a_broken_pointer_is_the_same_explicit_unavailable_everywhere` |
| R06 Hostname принимается формой, но отбрасывается сборщиком | `verified` | Дефект 10; `test_a_hostname_of_your_own_passes_the_whole_path` |
| R07 Watch только уменьшает пул | `implemented_unverified` | Дефект 12: refill по явному вызову есть (и в CLI, и в интерфейсе), периодического watch нет |
| R08 Анонимность может быть ложной | `implemented_unverified` | Дефект 13; оговорка про `test_anonymity.py:206-210` |
| R09 IPv6 DNSBL и коды не исправлены | `in_progress` (было `implemented_unverified`) | Дефект 14: `probes.py` починен; `reputation.py` переписан параллельной правкой в 12:01 и прежние разворот IPv6 и правило «любой `127.*` = listed» из него ушли (проверено в 12:17). Живых DNSBL-запросов не было, правка не закоммичена |
| R10 Измерение Mbps прежнее | `verified` | Дефект 15; `INSUFFICIENT_SAMPLE` |
| R11 Gateway reservation и handshake deadline | `verified` | Дефект 16; `tests/test_gateway_reservation.py` |
| R12 LAN по умолчанию и тот же секрет | `implemented_unverified` | Дефект 18: секреты разведены, LAN из GUI всё ещё недоступен (`lan` не доходит до `gateway.Background`) |
| R13 Denylist не отзывает уже выданный пул | `implemented_unverified` | Дефект 19; отзыв admissions сделан, судьба открытых streams — отдельная policy |
| R14 sing-box нуждается в versioned validation | `in_progress` | Дефект 20: fail-closed есть, `client_target` недостижим — единственные непустые значения задаёт только тест |
| R15 Скачивание закрывает stream дважды | `implemented_unverified` | Дефект 21; проверено под стабами, не в браузере |
| R16 Источники в main улучшены лишь частично | `in_progress` (было `todo`) | Совпадает с дефектом 22: четыре маршрута и `source_quality` починены, каталог по-прежнему указывает на `main/sources.json`, `country_resolver` по-прежнему снимает снимок до сбора |
| R17 Smart presets и «живая лента» сильнее backend | `in_progress` | Совпадает с дефектами 24 и 25: presets починены с честным `not_proved`, живая лента — нет |
| R18 Быстрый тест — диагностический | `implemented_unverified` | Дефект 23 |
| R19 Desktop-цель не закрыта | `external_blocker` | F23/дефект 26: per-user пути сделаны, `.app` собрана, но на версии 2.2.1; Windows не собирался; подписи нет. **Плюс F22**: слой `desktop.py` написан, но helper не попадает в bundle, поэтому в собранном приложении меню-бара нет |
| R20 Инженерная приёмка и документация отстают | `implemented_unverified` | CHANGELOG `[Unreleased]` заполнен, версия 2.3.0, README обновлены. **Осталось**: `tests/test_qr.py` проверяет текст, а не декодирование; browser E2E отсутствует; `SECURITY.md`/`CONTRIBUTING.md` не обновлены под `/v1`; README не знает про `--max-requests`, `--run-max-bytes`, `--count-what` и `bench` и описывает `--workers` как число воркеров |

---

## 4. A-F01–A-F40 — исходные 40 замечаний аудита

Дубли с MASTER-PROMPT §3 объединены без потери требования: в графе «Откуда»
указан ID, который несёт то же требование.

| ID | Откуда | Статус |
| --- | --- | --- |
| A-F01 URL каталога из `/main/sources.json` вместо package-path | R16 / дефект 22 | `in_progress` (было `todo`) — `branding.py:18` не тронут, но путь обновления, выбор источников и `source_quality` в приложении работают |
| A-F02 `scan()` пропускает старый успех без ограничения возраста | дефект 1, 2 | `verified` |
| A-F03 `--watch` только `recheck_passing=True` | дефект 12 / R07 | `implemented_unverified` |
| A-F04 GUI читает под эксклюзивным lock | дефект 5 / R03 | `implemented_unverified` (код `gui.py:245-246`, `:557-558`; сценарий во время scan мной не запускался) |
| A-F05 Перепроверка удаляет старые results до новых | дефект 6 | `verified` |
| A-F06 `country_resolver` копирует metadata до collect | дефект 22 | `in_progress` (было `todo`) — снимок `candidate_meta` по-прежнему снимается один раз при старте `main` (`proxytool.py:4830`) |
| A-F07 Общая таблица candidates не изолирует импорт | F02 / дефект 11 | `verified` |
| A-F08 `pending=total-checked` по всей БД | F10 | `implemented_unverified` |
| A-F09 `source_quality.passed` зависит от фильтров экспорта; prune удаляет | дефект 22 | `implemented_unverified` (было `todo`) — счётчик отделён от фильтров экспорта (`proxytool.py:2780-2790`, комментарий прямо это утверждает); `prune_sources` (`gui.py:1160`) правит только настройки. Живых прогонов с потерей источника я не делал |
| A-F10 Заслуга достаётся первому загрузившемуся источнику | F21 + SRC05 | `implemented_unverified` — `candidate_seen` даёт many-to-many происхождение, `compare_sources` честно делит разделяемое и называет цену копии (`tests/test_sourcedesk_compare.py`), но наружу не подключено |
| A-F11 `anonymity.classify` даёт elite без подтверждения | дефект 13 | `verified` в `probes.py` |
| A-F12 `min_anonymity` тихо заменяется на `any` | дефект 13 | `implemented_unverified` (тест `test_anonymity.py:206-210` не переписан) |
| A-F13 IPv6 DNSBL reverse с двоеточиями | дефект 14 | `implemented_unverified` — `probes.py` починен, а `reputation.py` в 12:01 переведён на `probes.reverse_ip`; правка не закоммичена и живым запросом не проверена |
| A-F14 Любой `127.*` = listed | дефект 14 | `implemented_unverified` — в текущем дереве правила нет (проверено в 12:17), но правка не закоммичена и живым запросом не проверена |
| A-F15 Mbps считает первый chunk, таймер после | дефект 15 | `verified` |
| A-F16 Fail-fast оставляет неполные samples | CHK07 / F05 | `implemented_unverified` |
| A-F17 `test_proxies` в обход pipeline и `screen_proxy` | F01 / ENG01 | `implemented_unverified` — движок ведёт конвейер (F12), но отдельные обходные входы в дереве остались |
| A-F18 «Рекомендуемые» используют редкость URL как гипотезу | F19 | `implemented_unverified` |
| A-F19 `hosting` — regex; страна по endpoint | F08 | `in_progress` |
| A-F20 `clean` по умолчанию; presets обещают лишнее | дефект 24 | `implemented_unverified` |
| A-F21 `DIRECT` в пустом Clash/sing-box | дефект 20 | `verified` (fail-closed) |
| A-F22 `Pool.pick()` до `acquire()` | дефект 16 | `verified` |
| A-F23 Deadline покрывает первый байт, не handshake | дефект 16 | `verified` |
| A-F24 `open_tunnel` ≠ успешный HTTP-запрос | дефект 17 | `implemented_unverified` |
| A-F25 Нет общего lifecycle у sessions/cache/usage | F16 | `implemented_unverified` |
| A-F26 `Exports.load` читает `ranked.json` и `status.json` раздельно | дефект 8 / 9 | `verified` |
| A-F27 GUI export не передаёт `q` и `result-hosting` | F19 / F08 | `in_progress` (единый критерий не внедрён в API/GUI) |
| A-F28 `downloadFile` закрывает stream дважды | дефект 21 | `implemented_unverified` |
| A-F29 Checker поддерживает HTTPS-to-proxy, gateway отбрасывает | F16 / CHK03 | `implemented_unverified` |
| A-F30 Denylist после TCP-prefilter | дефект 19 | `implemented_unverified` |
| A-F31 `public_source()` стирает path | дефект 22 | `implemented_unverified` (было `todo`) — `gui.py:143-153` добавляет к подписи короткий `core.source_key`, зеркала различимы; credentials при этом по-прежнему не показываются |
| A-F32 `normalize()` отбрасывает hostname/auth/private | F04 | `in_progress` (hostname — да, auth — нет) |
| A-F33 `paths.default_data()` для frozen пишет рядом с exe | F23 | `implemented_unverified` (`desktop` задаёт `PROXY_WORKBENCH_DATA`, но сырой frozen-CLI — нет) |
| A-F34 Тема/язык в localStorage случайного порта | UX14 | `todo` |
| A-F35 RAM не ограничена; API грузит полный JSON | ENG06/ENG07 | `in_progress` — `Budgets` теперь ограничивает RAM (`max_ram_bytes`, `ram_per_inflight_bytes`, `proxytool.py:1974-1982`), но legacy `/proxies` сохранил `MAX_LIMIT=1_000_000` |
| A-F36 Нет временной истории, миграций, именованных профилей | ENG03/ENG04 | `verified` (`db.py` миграции 0..15, `observations`, `profile_revision`; миграции 1..12 починены — `db.py:1148`) |
| A-F37 Нет ETag/cache, общего бюджета и deadline пагинации | SRC07/SRC09 | `implemented_unverified` (ETag/Last-Modified/304 есть; живых 304 я не наблюдал) |
| A-F38 Нет экрана отклонённых, 13 колонок, нет `role=progressbar` | F10 / UX11 | `in_progress` (CLI-диагностика есть, GUI-экрана нет) |
| A-F39 README противоречит флагам; AGENTS/CONTRIBUTING расходятся | F26 | `implemented_unverified` — `--workers` в README описан как число воркеров, новых флагов и `bench` там нет |
| A-F40 Нет browser E2E и packaged-Mac проверки; фиксированные порты | R20 / F23 | `implemented_unverified` (динамические порты в smoke — да; browser E2E — нет; packaged-Mac на текущей ревизии — нет) |

---

## 5. K-F01–K-F28 — 28 принятых карточек продуктового исследования

| ID | F | Статус | Приёмочный сценарий карточки |
| --- | --- | --- | --- |
| K-F01 | F01 | `implemented_unverified` | Частично: режимы в CLI есть, «найти 5 рабочих» не гонял |
| K-F02 | F02 | `verified` | Закрыт (`test_a_personal_import_after_a_big_public_sweep_checks_only_that_collection` + живой импорт в коллекцию через HTTP, §0.2) |
| K-F03 | F03 | `verified` | Закрыт на CLI (`test_a_repeated_import_commit_does_not_duplicate_membership`) и в интерфейсе: живой HTTP §0.2 — предпросмотр, commit с номерами отклонённых строк, идемпотентный повтор. Форматы `json`, режим `replace` и drag-and-drop не гонял |
| K-F04 | F04 | `in_progress` | Не закрыт: транспорт аутентификации отсутствует; canary-секрет доказан, перепривязка теперь поднимает ревизию (`ccb0954`) |
| K-F05 | F05 | `implemented_unverified` | Частично: parity профилей доказан, исполнение профиля GUI/CLI — нет |
| K-F06 | F06 | `implemented_unverified` | Частично: каталог виден в UI, diff обновления пользователю не показывается |
| K-F07 | F07 | `implemented_unverified` | Частично: единицы и лимиты есть; `allow_private` в `/v1/collections` по-прежнему игнорируется |
| K-F08 | F08 | `in_progress` | Не закрыт: один критерий для GUI/API/export не внедрён, схема не держит exit-модель |
| K-F09 | F09 | `implemented_unverified` | Закрыт на уровне admission; сквозной путь до GUI/gateway/new connection не гонял |
| K-F10 | F10 | `implemented_unverified` | Частично: CLI `diagnose zero\|funnel` объясняет; в GUI ничего нет; `diagnose health` печатает ложную тревогу (воспроизведено) |
| K-F11 | F11 | `verified` | Закрыт (`tests/test_jobs_*` + живой прогон с job ID) |
| K-F12 | F12 | `implemented_unverified` | Закрыт по стадиям, бюджетам, find-N и трём единицам (живой прогон §0.1). Не закрыт: `EXPENSIVE_UNTIL_N` не используется, поток `collect → проба` отсутствует, `--run-max-bytes` числом не подтверждён |
| K-F13 | F13 | `implemented_unverified` | Закрыт по четырём маршрутам источников (живой HTTP §0.2). Не закрыт: `source_feed`/`membership_source` не создаются в живой базе, живых fetch/304 не было |
| K-F14 | F14 | `implemented_unverified` | Закрыт по refill в CLI и в интерфейсе; не закрыт по автономному watch |
| K-F15 | F15 | `implemented_unverified` | Закрыт по DST/слиянию слотов после сна/бюджетам/уведомлениям, и `mark_wake`/`mark_awake` теперь вызываются из `desktop.py`; не закрыт по переживанию перезапуска — в таблице `schedules` нет `last_run_at`/`paused` |
| K-F16 | F16 | `implemented_unverified` | Частично: reservation/auth/лимиты, привязка настраивается в интерфейсе; LAN из GUI недоступен, `Binding` до слушателя не доходит, `--pool` в CLI нет |
| K-F17 | F17 | `implemented_unverified` | Не закрыт: контроль маршрута отсутствует, round-trip QR не проверяется |
| K-F18 | F18 | `implemented_unverified` | Закрыт на CLI+API+GUI (живой HTTP §0.2 показывает общие вызовы); `tests/test_parity` отсутствует |
| K-F19 | F19 | `implemented_unverified` | Закрыт в части «выделенное не переносится на другой scope» (три теста). Теги, заметки, saved views и матрица теперь написаны, но мной не вызывались |
| K-F20 | F20 | `in_progress` | Менялось в ходе сессии: в 11:56 WS/media были объявлены неподдерживаемыми, в 12:16 реализованы (`run_websocket`, `run_duration`, `run_media` + эндпоинты reference probe). Прогонов этих проб не было, и `tests/test_probes_reference.py:218` на них падает |
| K-F21 | F21 | `implemented_unverified` | Функция написана и покрыта 43 тестами, но **ни к одной поверхности не подключена** — карточка не закрыта |
| K-F22 | F22 | `implemented_unverified` | Слой `desktop.py` написан и, по отчёту владельца, проверен на macOS; в bundle helper не кладётся, Windows/Linux не проверялись, в веб-интерфейсе фоновые события не показываются |
| K-F23 | F23 | `external_blocker` | Не закрыт на текущей ревизии |
| K-F24 | F24 | `verified` | Закрыт в CLI (включая миграции 1..12 и копию перед неаддитивным шагом — живое подтверждение в §F24) и в интерфейсе (предпросмотры, §0.2) |
| K-F25 | F25 | `implemented_unverified` | Частично: bundle в CLI, в GUI нет; `diagnose health` сломан |
| K-F26 | F26 | `implemented_unverified` | Частично: CHANGELOG/README/version обновлены; SECURITY/CONTRIBUTING — нет; README отстаёт по флагам; 10 языков откатываются на английский |
| K-F27 | F27 | `implemented_unverified` | Не закрыт: `REQUESTED_DDL` не исполняется, живых fetch не было |
| K-F28 | F28 | `implemented_unverified` | Частично: snapshot contract и selection-артефакт; `client_target` недостижим |

---

## 6. 140 задач BACKLOG.ru.md

### 6.1 Каталог и сбор источников (SRC01–SRC20)

| ID | Статус | Примечание |
| --- | --- | --- |
| SRC01 каталог с ID/publisher/family/parser | `implemented_unverified` | `sourcedesk.py` + `sources.json` |
| SRC02 карточки источников, фильтры | `implemented_unverified` | В CLI/API/интерфейсе есть; карточки каталога по-прежнему не полны |
| SRC03 журнал fetch | `implemented_unverified` | `feed_diagnostics` в `sourcedesk`; GUI-экран отсутствует |
| SRC04 раздельные счётчики raw/valid/unique/new/passed | `implemented_unverified` | Есть в отчётах источников |
| SRC05 many-to-many provenance, first/last-seen | `implemented_unverified` | `candidate_seen` даёт many-to-many; `membership_source` из `REQUESTED_DDL` в живой базе не создаётся |
| SRC06 группировка зеркал и dataset-family | `implemented_unverified` | Поле family в каталоге; `compare_sources` группирует одинаковые наборы в семейства (тесты) |
| SRC07 ETag/Last-Modified/304/cache | `implemented_unverified` | Заявлено в `sourcedesk`; живых 304 я не наблюдал |
| SRC08 per-host concurrency, Retry-After, backoff | `implemented_unverified` | Есть в сборщике |
| SRC09 общий бюджет сбора | `implemented_unverified` | `CHANGELOG`: 32 МиБ / 500 000 кандидатов на источник |
| SRC10 карантин вместо prune | `in_progress` | `quarantine_until` есть в `REQUESTED_DDL`; `prune_sources` правит только настройки, экран карантина в GUI нет |
| SRC11 обновление каталога с preview/overrides/rollback | `todo` | `api.py` отдаёт каталог; `branding.py:18` по-прежнему `main/sources.json`; `update_sources` живого fetch не выполнял |
| SRC12 подписанный manifest каталога | `todo` | Не реализован |
| SRC13 мастер добавления URL с предпросмотром | `in_progress` (было `todo`) | `POST /api/sources/add` (`gui.py:641`) добавляет URL с выбранным форматом и отвечает 200, отвергая не-HTTP(S) адрес и loopback понятным текстом; полноценного мастера с предпросмотром нет |
| SRC14 JSON/CSV adapters с картой полей | `implemented_unverified` | `sourcedesk` adapters есть |
| SRC15 структурный HTML-table parser, IPv6, BOM/CRLF | `implemented_unverified` | Адаптеры покрывают форматы; устойчивость к смене разметки не проверялась |
| SRC16 полный Geonode adapter с hints | `in_progress` | Гео после collect — A-F06, не исправлено |
| SRC17 наборы «основной/SOCKS5/расширенный/свои» | `todo` | Не реализованы |
| SRC18 адаптивный выбор по уникальному вкладу | `implemented_unverified` (было `todo`) | Расчёт есть в `compare_sources`/`compare_cohorts` с покрытием тестами, но выбора источников по вкладу продукт не делает и наружу не отдаёт |
| SRC19 паспорт источника (homepage, terms, attribution) | `implemented_unverified` | Поля в каталоге |
| SRC20 процесс предложения новых sources | `todo` | Не реализован |

### 6.2 Собственные списки и импорт (IMP01–IMP10)

| ID | Статус | Примечание |
| --- | --- | --- |
| IMP01 именованные коллекции с отдельным составом | `verified` | F02 |
| IMP02 явный scope запуска | `verified` | F02, дефект 11 |
| IMP03 owned endpoints с hostname/credentials | `in_progress` | hostname — да; credentials — отвергаются (F04) |
| IMP04 OS secret store и `auth_ref` | `implemented_unverified` | `secrets.py` vault/session; OS-vault не проверен вживую |
| IMP05 импорт URI/host:port/CSV/JSON через mapper | `verified` | Живой HTTP: `POST /api/import/preview` с CSV и сопоставлением колонок вернул 200 с `mapping` и `columns` |
| IMP06 отчёт импорта с номерами строк | `verified` | Живой HTTP: `POST /api/import/commit` вернул `rejected: [{line: 2, E_IMPORT_PRIVATE}, {line: 3, E_IMPORT_MISSING_FIELD}, …]` |
| IMP07 несколько файлов и вставка из буфера с preview | `implemented_unverified` | `from_text`, `from_drop`; интерфейсная карточка импорта читает файл и буфер и показывает предпросмотр — живым HTTP проверен буфер, выбор файла в браузере нет |
| IMP08 merge/replace и diff состава | `implemented_unverified` | `test_a_repeated_import_commit_does_not_duplicate_membership`; в интерфейсе commit отвечает `replayed: true` при повторе. Режим `replace` не гонял |
| IMP09 импорт Clash/sing-box через data-only adapters | `implemented_unverified` | `import_clash`/`import_singbox` без исполнения rules |
| IMP10 привязка локального файла с обновлением | `todo` | Не реализован |

### 6.3 Проверки и качество измерений (CHK01–CHK18)

| ID | Статус | Примечание |
| --- | --- | --- |
| CHK01 общий pipeline для CLI/GUI/scheduler | `implemented_unverified` | F12: конвейер ведёт прогон CLI; GUI запускает тот же движок, но `scheduler.py` не вызывает `scan` напрямую — автоматический запуск по расписанию живого скана я не наблюдал |
| CHK02 структурные стадии и коды ошибок | `implemented_unverified` | Стадии и коды приходят из `pipeline.py`; живой прогон дал `E_LIMIT_BUDGET`, `want_unreachable_exit`, `want_unreachable_ip` |
| CHK03 capability matrix | `implemented_unverified` | `probes.capability_matrix()`; GUI не показывает |
| CHK04 раздельные connect/handshake/TLS/TTFB/download | `implemented_unverified` | Есть в `probes.py` |
| CHK05 число наблюдений и уверенность | `in_progress` | Частично в `core.history_of` |
| CHK06 JSON assertions, content-type, contains | `implemented_unverified` | Есть в профиле; заглушка с 200 всё ещё проходит несовместимую структуру |
| CHK07 обязательные/необязательные targets, AND/OR, пороги | `implemented_unverified` | `profiles.py` all/any/at_least_k |
| CHK08 предварительная диагностика и circuit breaker | `implemented_unverified` | `diagnostics.check_control` (живой путь не выполнялся) |
| CHK09 TLS diagnostics и custom CA для owned | `implemented_unverified` | TLS-проверка по умолчанию не отключается; custom CA за вестимым scope есть |
| CHK10 ограниченные redirects с журналом цепочки | `implemented_unverified` | `max_redirects` в бюджетах |
| CHK11 cold и reused соединения раздельно | `todo` | Не реализовано |
| CHK12 judge adapters с nonce/schema, unknown | `verified` | Дефект 13 |
| CHK13 DNSBL zone adapters, IPv6, bounded cache | `in_progress` (было `implemented_unverified`) | Дефект 14: `probes.py` закрыл код, `reputation.py` переведён на него в 12:01; правка не закоммичена, живых запросов не было |
| CHK14 раздельные endpoint/exit геолокация, ASN | `implemented_unverified` | F08; не внедрено в API/GUI |
| CHK15 самостоятельно размещаемый echo/judge | `implemented_unverified` | `probes.serve_reference_probe` |
| CHK16 корректный bandwidth с min-duration | `verified` | Дефект 15 |
| CHK17 WebSocket и длительное соединение | `in_progress` (было `todo`) | В 12:16 реализованы в `probes.py` вместе с media; результатов проб я не получал, тест матрицы на них падает. Не поддерживаются по-прежнему только `udp_transport` и `http2_or_http3` |
| CHK18 лимиты на target, обработка 429 | `implemented_unverified` | Лимиты есть; judge получает не больше половины общего бюджета (`proxytool.py:1988-1993`); 429-политика проверена на фикстурах |

### 6.4 Списки результатов (RES01–RES15)

| ID | Статус | Примечание |
| --- | --- | --- |
| RES01 fresh/stale/failed/unknown и «проверено N минут назад» | `verified` | `test_expired_and_unknown_rows_are_reachable_by_name` |
| RES02 выбор строк и групповые операции | `implemented_unverified` | 26 вхождений `bulk` в `app.js` |
| RES03 сохранённые представления и фильтры | `implemented_unverified` (было `todo`) | `gui.py:2599-2629` `saved_views`/`store_view`/`delete_view`, сайдкар `gui-views.json`, маршруты `GET/POST /api/views`, `/api/views/delete`. Не вызывались мной |
| RES04 карточка прокси с происхождением | `implemented_unverified` | Есть карточка деталей; объяснение допуска по core-причине частичное |
| RES05 матрица proxy × target | `implemented_unverified` (было `todo`) | `gui.py:2260` `matrix`, маршрут `GET /api/results/matrix` |
| RES06 отдельный список отклонённых | `in_progress` | `diagnose zero`/`funnel` в CLI; в GUI нет |
| RES07 история latency, passes, exit-IP | `implemented_unverified` | `core.history_of`; таблица не показывает |
| RES08 объяснение рекомендации | `implemented_unverified` | `proxytool.py:2568` `recommender` |
| RES09 компактные колонки по умолчанию | `verified` | `gui.py` `COMPACT_COLUMNS` |
| RES10 фильтры источник/коллекция/ASN/CIDR/теги | `implemented_unverified` (было `in_progress`) | Фильтры и теги есть; теги живут в `gui-annotations.json` и используются в массовых операциях |
| RES11 группировка по host/exit-IP без потери endpoint | `implemented_unverified` | Миграция 15: одна строка `results` на адрес |
| RES12 избранное, заметки, теги, pin | `implemented_unverified` (было `todo`) | Избранное, заметки и теги написаны (`gui.py:2493-2514`, `:2191`, `:2345-2346`). **`pin` не найден**: `grep -n "'pin'\|pin=" proxy_workbench/gui.py` даёт только комментарий, реализации закрепления нет |
| RES13 сравнение запусков | `todo` | Не реализовано (сравнение источников из F21 — другое) |
| RES14 единый экспорт текущей выборки с preview | `implemented_unverified` | `kind='selection'` + счётчики |
| RES15 копирование в URI/host:port/tool-specific | `implemented_unverified` | Есть; пароль — только отдельным действием |

### 6.5 Пул и автоматизация (POO01–POO12)

| ID | Статус | Примечание |
| --- | --- | --- |
| POO01 «поддерживать N» с watermark | `implemented_unverified` | Refill по явному вызову есть в CLI, API и интерфейсе (живой HTTP); автономного цикла нет |
| POO02 повторный допуск после cooldown | `verified` | `pools.py`; `tests/test_pools_lifecycle.py` |
| POO03 раздельные расписания source refresh и recheck | `implemented_unverified` | `scheduler.py`; в интерфейсе расписания появились, разделения видов работ по расписанию я не проверял |
| POO04 очередь заданий по профилям и scope | `verified` | `jobs.py` + `WriterGate` |
| POO05 бюджеты времени/трафика/requests | `implemented_unverified` (было `verified`) | **Понижен из `verified`, потому что доказательства больше нет**: `--max-requests` проверен живым прогоном (`checked: 1, requests: 1, E_LIMIT_BUDGET`), а `--run-max-bytes` на моей фикстуре не сработал (`bytes: 0`), поэтому «бюджеты платят и то, и другое» подтверждено наполовину |
| POO06 checkpoint/pause/resume без потери истории | `verified` | Дефект 6 |
| POO07 уведомления без шума | `implemented_unverified` | `scheduler.Deduplication` в тестах; системные каналы (меню-бар) появились только на macOS и не в собранном приложении |
| POO08 интервал recheck по живучести | `todo` | Нет счётчика отказов в `pool_member` |
| POO09 offline/sleep/wake/network-change | `implemented_unverified` (было `todo`) | `desktop.py` `PlatformObserver`/`network_fingerprint`/`wake_tick` вызывают `scheduler.mark_wake` и `jobs.mark_awake`; проверено владельцем на macOS, мной — нет; Windows/Linux не проверялись |
| POO10 объяснение недобора и preview ослабления | `implemented_unverified` | `deficit_reason` в интерфейсе отдаётся (живой HTTP); полной разбивки `deficit_reasons[]` нет |
| POO11 закреплённые sessions strict/failover | `implemented_unverified` | `gateway.py` sticky-режимы |
| POO12 агрегаты health-событий gateway | `implemented_unverified` | Есть, но не вызывают внеочередную проверку из GUI |

### 6.6 API и подключения (API01–API12)

| ID | Статус | Примечание |
| --- | --- | --- |
| API01 `/v1`, OpenAPI, стабильные коды | `verified` | `openapi.json` генерируется из таблицы маршрутов; функциональная приёмка получила HTTP 200/202 на 13 операциях |
| API02 общие filters/sort/max-age | `implemented_unverified` | Фильтры разные на legacy `/proxies` и `/v1` |
| API03 cursor pagination, limits, ETag | `verified` | `MAX_PAGE_LIMIT=1000`; `test_an_event_stream_answers_with_frames_and_stops_on_its_own` |
| API04 health/readiness с причинами empty/stale | `implemented_unverified` (было `verified`) | **Понижен**: `diagnose health` печатает ложную тревогу (воспроизведено: `5 checks, 5 problems` при одном реальном отказе), а `reader_state` в API отвечает корректно. Формулировка «health с причинами» пользователю не выдаётся верно |
| API05 lease/acquire/release с TTL | `in_progress` | `reservations.lease/acquire/release` вызывают один `_reservation`, который читает текущий снимок и отдаёт первые N строк; `lease_id`, `ttl_s` и `state` из тела **игнорируются**, ничего не бронируется и не освобождается |
| API06 bounded client feedback | `todo` | Не реализован |
| API07 SSE/events о заданиях | `verified` | `test_an_event_stream_answers_with_frames_and_stops_on_its_own` |
| API08 подписки PAC/Clash/sing-box с ETag | `implemented_unverified` | `exportsvc`; клиентская проверка версии не достижима (`client_target` пуст) |
| API09 проверенные примеры curl/Python/JS | `in_progress` | Полный сценарий есть в docstring `apiv1.py`; в README/SECURITY не перенесён |
| API10 именованные пулы/порты для приложений | `implemented_unverified` | `Binding.pool_id`; в интерфейсе привязка настраивается, но `Binding` не передаётся в слушатель; `--pool` в CLI нет |
| API11 единые upstream transports | `implemented_unverified` | capability matrix; HTTPS-to-proxy parity в gateway не завершена |
| API12 token scopes, rotation, TLS reverse proxy | `implemented_unverified` (было `verified`) | Скоупы, ротация и отзыв работают (живой HTTP §0.2), `test_the_legacy_token_reads_and_gains_nothing` зелёный. **Понижен**: `POST /v1/keys` с повторным `Idempotency-Key` возвращает полный секрет второй раз — это прямо ломает «секрет показывается один раз» для HTTP-поверхности |

### 6.7 Удобство интерфейса (UX01–UX15)

| ID | Статус | Примечание |
| --- | --- | --- |
| UX01 первый запуск без терминала | `in_progress` | Текст справки объясняет сценарии; мастер выбора цели отсутствует |
| UX02 простой и расширенный режимы | `verified` | Переключатель в UI есть |
| UX03 именованные шаблоны с точным описанием | `implemented_unverified` | `servicecatalog` + `not_proved` |
| UX04 мастер подключения и «Проверить подключение» | `in_progress` | Recipes есть; контроль маршрута (last_proxy/pin) отсутствует |
| UX05 понятные пустые/ошибочные состояния | `in_progress` | В интерфейсе сообщения с действием появились (живой HTTP: `«Укажите источник.», «Не сопоставлена обязательная колонка "host".»`), но `diagnose zero`/`funnel` в GUI отсутствуют |
| UX06 structured i18n с ключами | `implemented_unverified` | `error.langPack`; 12 языков, но новые ключи есть только в en/ru |
| UX07 вынос локализаций | `implemented_unverified` | `ui/i18n/*.js`; проверка ключей — `tests/test_i18n.py` |
| UX08 loading/offline/reconnecting и черновики | `todo` | Не реализовано |
| UX09 клавиатурные команды | `todo` | Не реализовано |
| UX10 клавиатурная навигация и фокус | `todo` | Не реализовано |
| UX11 доступный прогресс для screen reader | `todo` | Не реализовано |
| UX12 адаптация 1366×768, 200% zoom, reduced motion | `todo` | Не реализовано |
| UX13 отмена устаревших запросов и debounce | `todo` | Не реализовано |
| UX14 сохранение темы/языка/колонок/профиля | `todo` | A-F34 не исправлен: localStorage привязан к порту случайного запуска |
| UX15 настройки с preview, backup/reset, диагностика | `implemented_unverified` | **Часть закрыта**: предпросмотр уборки, retention и восстановления есть в интерфейсе (живой HTTP §0.2). **Не закрыто**: GUI-диалога диагностики и пакетного диагностического bundle по-прежнему нет |

### 6.8 Desktop и установка (DES01–DES14)

| ID | Статус | Примечание |
| --- | --- | --- |
| DES01 per-user data/cache/logs и portable mode | `implemented_unverified` | `desktop.resolve_layout`; `paths.default_data` для сырого frozen по-прежнему рядом с exe |
| DES02 самодостаточная Mac arm64 `.app` и `.dmg` | `implemented_unverified` | Артефакт версии 2.2.1 от 25.09 существует; на текущей 2.3.0 не пересобран, `PyInstaller` отсутствует |
| DES03 Intel/universal2 | `external_blocker` | `build_macos.py:55-58` намеренно отказывает; PyInstaller отсутствует |
| DES04 Windows installer per-user и portable ZIP | `external_blocker` | `windows-installer.iss` написан по документации, ни разу не собран |
| DES05 native desktop window | `todo` | Тонкий desktop host поверх текущего UI; нативного окна нет |
| DES06 иконки, metadata, About, версии | `implemented_unverified` | Метаданные есть, иконка/Dock не проверены |
| DES07 tray/menu bar | `implemented_unverified` (было `todo`) | `desktop.py` `TRAY_HELPER_SOURCE`/`ensure_tray_helper`/`Tray` — слой написан и, по отчёту владельца, проверен на macOS. **В собранном `.app` его нет**: `build_macos.py` и все четыре `.spec` не кладут helper в bundle, поэтому frozen-сборка честно сообщает «в сборке нет helper меню-бара» |
| DES08 single instance с безопасным IPC | `implemented_unverified` (было `todo`) | `InstanceLock`, `instance_lock_path:1291`, `ControlServer`/`control_request` — слой написан; повторный запуск и IPC мной не проверялись |
| DES09 lifecycle workers и завершение | `implemented_unverified` | `desktop` закрывает handlers; `Tray.reap()`/`Tray.stop()` и `interface_holder()` описаны в handoff; проверка на собранном `.app` относится к старой ревизии |
| DES10 автозапуск по явному выбору | `implemented_unverified` (было `todo`) | `autostart_status:1606`, `enable_autostart:1672`, LaunchAgent / `HKCU\…\Run` / XDG. macOS-путь описан и, по отчёту владельца, проверен; **Windows-ветка (`winreg`) и XDG не запускались ни разу** |
| DES11 обновления с проверкой подписи и rollback | `implemented_unverified` | `desktop.plan_update`/`apply_update`/`rollback` — preview по умолчанию; публикующего сервера нет |
| DES12 signing и notarization в release pipeline | `external_blocker` | Ключей нет, не создавались и не покупались; манифест `"signed": false`; `release_manifest.py` берёт флаг только из реальной проверки подписи |
| DES13 установочные проверки чистой системы | `external_blocker` | Нет Windows-машины; macOS-установка на текущей ревизии не проверялась (артефакт 2.2.1) |
| DES14 системный proxy (P3) | `not_applicable_with_evidence` | `BACKLOG.ru.md:221` относит системный proxy/TUN к «за пределами плана релиза»; MASTER-PROMPT §0 запрещает включать системный proxy на машине пользователя в эту реализацию |

### 6.9 Архитектура, данные и качество (ENG01–ENG14)

| ID | Статус | Примечание |
| --- | --- | --- |
| ENG01 разделить collector/checker/storage/jobs/selection/export | `verified` | 40 доменных модулей в `proxy_workbench/*.py`; движок импортирует их, а прогон ведёт `pipeline.Pipeline` |
| ENG02 типизированные модели | `implemented_unverified` | Dataclass-ы есть; полной schema-проверки нет |
| ENG03 observations и latest summaries | `verified` | `observations` (миграция 4), одна строка `results` на адрес (миграция 15) |
| ENG04 версионированные migrations, backup, restore | `verified` | `db.py` миграции 0..15, `VACUUM INTO`, restore/rollback в CLI, **починка миграций 1..12** (`db.py:1148`) и живое подтверждение копий `v0`/`v14` (§F24) |
| ENG05 retention и cleanup с preview | `verified` | `retention_preview`/`apply_retention` считают один план (`_retention_targets`), заблокированные строки считаются нулём; `cleanup_preview` + `backup retention\|cleanup --apply`; в интерфейсе предпросмотры отвечают 200 (живой HTTP) |
| ENG06 индексы и materialized summaries | `implemented_unverified` | Индексы миграции 14; `MAX_PAGE_LIMIT` для `/v1` |
| ENG07 ограничить RAM и число соединений | `implemented_unverified` (было `in_progress`) | `Budgets` теперь тратит дескрипторы и RAM по-настоящему (`proxytool.py:1974-1982`, `acquire(fds=…, ram=…)`), потолок `worker_ceiling()` двигается по наблюдаемой доле успеха. Живого измерения потолка RAM/FD я не делал |
| ENG08 общая schema validation | `implemented_unverified` | `E_VALIDATION_*`; `allow_private` — контрпример (не изменился) |
| ENG09 regression/contract suite | `verified` | `Ran 2480 tests`, `OK (skipped=1)`, код выхода 0 |
| ENG10 browser E2E + accessibility | `todo` | Не реализовано; драйвера браузера в окружении нет |
| ENG11 performance benchmarks с baseline | `implemented_unverified` | `pipeline.benchmark` (синтетика, честно помечена) и команда `bench` в CLI; живых замеров нет. **Побочно найдено:** `db.upsert_endpoint` выполняет `PRAGMA table_info` на каждый вызов (замерено 7 мкс на `columns()`), кэша нет |
| ENG12 resource limits и trust-boundary review | `implemented_unverified` | Лимиты тела/очереди/rate в `/v1`; `test_apiv1_limits.py` зелёные |
| ENG13 CLI subcommands, JSON output, exit codes | `verified` | Приёмка вызывала `collect`, `api-key bootstrap`, `serve`, `backup`; `test_the_cli_reaches_the_import_the_pools_the_schedules_and_the_backups`; живой прогон §0.1 и `backup` в этой сессии |
| ENG14 структурные логи, correlation IDs, redaction | `implemented_unverified` | `test_the_canary_secret_is_nowhere`; полный diagnostic bundle только в CLI |

### 6.10 Open source и распространение (OSS01–OSS10)

| ID | Статус | Примечание |
| --- | --- | --- |
| OSS01 README с кнопками Windows/Mac и тремя сценариями | `implemented_unverified` | README обновлены; раздельных бинарных кнопок нет |
| OSS02 единый статус beta/stable, capability matrix | `implemented_unverified` | Версия 2.3.0 единая; матрица возможностей есть в `probes.py` |
| OSS03 Roadmap с milestones и критериями | `implemented_unverified` | `06-roadmap-and-release.ru.md` — исследование, не обновлённый roadmap репозитория |
| OSS04 guide для нового source/parser | `todo` | Не реализован |
| OSS05 translation guide и синхронизация EN/RU | `in_progress` | 12 языков в интерфейсе, но **en/ru содержат 1143/1217 ключей, а каждый из десяти пакетов — 791**, то есть свыше 350 ключей отсутствуют и откатываются на английский; guide в `docs/` нет |
| OSS06 draft release после успешных сборок | `external_blocker` | Windows не собирался; macOS-артефакт отстаёт на десятки коммитов |
| OSS07 dependency/SBOM/checksums/provenance | `implemented_unverified` | `packaging/release_manifest.py`, `verify_release.py` — для macOS-сборки 2.2.1 |
| OSS08 публикация PyPI | `todo` | Не выполнялась (публикация вне scope) |
| OSS09 Homebrew/winget/Linux multiarch | `todo` | Не реализовано |
| OSS10 согласованные CONTRIBUTING/AGENTS/SECURITY/PRIVACY | `in_progress` | Документы есть; под `/v1`, менеджер ключей и новые флаги не обновлены |

---

## 7. X01–X08 и отвергнутые R-идеи (03-idea-catalog.ru.md)

| ID | Идея | Оценка | Статус | Чем закрыто |
| --- | --- | --- | --- | --- |
| X01 | Browser extension | Не реализовывалась: расширение добавляет разрешения магазина и политику браузера, а F17 закрывается существующими страницами, recipes и QR | `not_applicable_with_evidence` | `03-idea-catalog.ru.md:75`; F17 частично не закрыт (контроль маршрута, round-trip QR) — известная граница, а не обязательство написать extension |
| X02 | Удалённое управление | **Обязательная часть = F29**: отдельные identity, audit log, revocation/expiry, LAN opt-in | `implemented_unverified` | `functional_acceptance.py` подтвердил работу `/v1` с реальным ключом; менеджер ключей в интерфейсе проверен живым HTTP (§0.2). LAN из GUI всё ещё недоступен (дефект 18) |
| X03 | Мобильный web-доступ к управлению | Не реализовывался: адаптивный UI для одного узла — это control plane, а не проксирование трафика телефона | `not_applicable_with_evidence` | `03-idea-catalog.ru.md:77`; обязательной части в F01–F29 нет |
| X04 | Дополнительный транспорт | HTTPS-to-proxy поддержан checker'ом; gateway parity не завершена | `in_progress` | `probes.capability_matrix()` фиксирует статус каждого транспорта; A-F29 |
| X05 | Самостоятельные проверочные endpoints | **Обязательная часть = F20**: self-hosted reference probe реализован | `verified` | `probes.reference_probe_targets:2248`, `probes.serve_reference_probe:2320`; локальные positive/negative сценарии в тестах |
| X06 | VPN/TUN-адаптер внешнего движка | Не реализовывался: XL+, требует прав, DNS, routes, исключений, UDP и отдельного продукта | `not_applicable_with_evidence` | `03-idea-catalog.ru.md:80`; `BACKLOG.ru.md:221` |
| X07 | Синхронизация конфигураций | Не реализовывалась: обязательное облако отклонено, аккаунт вне scope | `not_applicable_with_evidence` | `03-idea-catalog.ru.md:81`; F24 даёт несекретный backup/restore как эквивалентный закрытый сценарий переноса |
| X08 | Поставщик с управляемыми session/rotation API | Не реализовывался: нет выбранного пользователями поставщика, покупка запрещена | `not_applicable_with_evidence` | `03-idea-catalog.ru.md:82`; эквивалентный закрытый сценарий — F27 `import_subscription` с secret-reference и redaction |
| R01 (идея) | Собственный VPN/TUN стек | Отклонена как отдельный сетевой продукт | `not_applicable_with_evidence` | `03-idea-catalog.ru.md:88` |
| R02 (идея) | Обязательный аккаунт/облако/телеметрия | Отклонена: противоречит локальному OSS | `not_applicable_with_evidence` | `03-idea-catalog.ru.md:89`; `PRIVACY.md` фиксирует отсутствие телеметрии |
| R03 (идея) | Native мобильный клиент с проксированием телефона | Отклонена: два новых OS, другой сценарий | `not_applicable_with_evidence` | `03-idea-catalog.ru.md:90` |

---

## 8. Сквозная приёмка §7 (22 сценария)

| № | Статус | Чем подтверждено |
| --- | --- | --- |
| 1 новичок без URL находит базово пригодные | `implemented_unverified` | Путь доказан на локальных заглушках и живым прогоном §0.1 с явным `--url`; живые публичные прокси не измерялись |
| 2 страна по имени, endpoint/exit, unknown одинаково | `implemented_unverified` | `test_country_by_name_and_unknown_behave_the_same_in_every_interface`; API/GUI фильтруют страну своей строкой (F08) |
| 3 all/any/K даёт раздельные результаты | `verified` | `test_all_any_and_k_services_give_different_results` |
| 4 изменение профиля не смешивает версии измерений | `implemented_unverified` | `profile_revision` в `observations`; сквозного прогона через GUI не делал |
| 5 свой импорт после публичного сбора | `verified` | `test_a_personal_import_after_a_big_public_sweep_checks_only_that_collection` + живой импорт в коллекцию через HTTP (§0.2) |
| 6 CSV/JSON/TXT preview, errors, duplicates, cancel, replace, повтор | `implemented_unverified` | Закрыт на CLI и на CSV-пути интерфейса (живой HTTP §0.2: предпросмотр, номера отклонённых строк, повтор без дублей). Формат `json`, режим `replace` и отмена в браузере не гонялись |
| 7 hostname/auth полный путь, смена пароля | `in_progress` | `test_a_hostname_of_your_own_passes_the_whole_path`, `test_a_password_change_revokes_the_previous_proof`; перепривязка теперь поднимает ревизию (`ccb0954`); транспорт аутентификации отсутствует |
| 8 источники 200/304/429/partial, bad update не стирает last-good | `implemented_unverified` | Проверено на подставных `FeedResult`; живого fetch не было, и `source_feed` в живой базе не создаётся |
| 9 большой источник ограничен, первые результаты, stop/resume | `implemented_unverified` | Бюджеты и стадии работают (живой прогон §0.1, `E_LIMIT_BUDGET`); поток `collect → проба` отсутствует (F12) |
| 10 recheck/cancel/crash, timestamps, mixed-age, corrupt pointer | `verified` | Дефекты 2, 3, 4, 6, 9; соответствующие тесты зелёные |
| 11 экспорт выбранных не переключает активный пул | `verified` | Дефект 7; три теста |
| 12 empty/unsupported/expired не выбирает DIRECT | `implemented_unverified` | `test_an_empty_export_never_selects_a_direct_route`; клиентская валидация версии недостижима |
| 13 Maintained N восстанавливается, нехватка объясняется | `implemented_unverified` | `tests/test_pools_lifecycle.py` + живой `POST /api/pools/action refill` → 200 и `deficit_reason` в `GET /api/pools`; автономного watch нет |
| 14 gateway: concurrent max-per-proxy, partial handshake, cancel, shutdown | `verified` | `tests/test_gateway_reservation.py`, `test_gateway_bind.py`, `test_gateway_health.py` |
| 15 read-only и operator keys, scopes, revocation, SSE, quotas | `implemented_unverified` | Скоупы, разграничение и отзыв подтверждены приёмкой и живым HTTP §0.2. **Квоты параллелизма не работают** (`Principal.concurrency` только записывается, `QuotaGuard` без вызовов), а `POST /v1/keys` по повторному `Idempotency-Key` отдаёт секрет второй раз |
| 16 полный API-сценарий внешним клиентом без GUI | `verified` | `test_one_bootstrap_key_finishes_the_whole_journey_without_the_gui`; функциональная приёмка прошла целиком через CLI+HTTP |
| 17 QR декодируется, recipes проверяемы, роли различаются | `in_progress` | Рецепты есть; round-trip декодирование QR по-прежнему не проверяется — `tests/test_qr.py` смотрит в исходник (R20) |
| 18 Windows/Mac запуск без Python, ресурсы, второй запуск, tray | `external_blocker` | F23/дефект 26 + F22: артефакты отстают, Windows не собирался, подписи нет, а в собранном `.app` не будет и меню-бара |
| 19 сон/смена сети/vault не повреждают историю | `in_progress` | `desktop.py` вызывает `scheduler.mark_wake` и `jobs.mark_awake`; по отчёту владельца macOS-проверки пройдены. Мной не перепроверено, Windows/Linux не проверялись; интервальная сетка не переживает перезапуск (в `schedules` нет `last_run_at`/`paused`) |
| 20 backup/migration/rollback/retention безопасны, канарейки не текут | `verified` | F24 + живое подтверждение: `python -m proxy_workbench backup` перечислил копии `v0`/`v14` с манифестами; `test_the_canary_secret_is_nowhere` |
| 21 RU/EN, клавиатура, screen reader, 200% zoom, браузеры | `in_progress` | 12 языков, но новые ключи только в en/ru (en 1143 / ru 1217 / пакеты по 791); клавиатура, screen reader, zoom не проверялись |
| 22 35 сценариев исследования сопоставлены | `verified` | Раздел 5: 28 карточек сопоставлены 1:1, X01–X08 и отвергнутые идеи — раздел 7 |

---

## 9. Сводка по F01–F29 и дефектам 1–26

| Статус | F01–F29 | Дефекты 1–26 | Всего (55) |
| --- | --- | --- | --- |
| `verified` | 4 — F02, F03, F11, F24 | 12 — 1, 2, 3, 4, 6, 7, 9, 10, 11, 13, 15, 16 | **16** |
| `implemented_unverified` | 21 | 10 — 5, 8, 12, 17, 18, 19, 20, 21, 23, 24 | **31** |
| `in_progress` | 3 — F04, F08, F20 | 3 — 14, 22, 25 | **6** |
| `todo` | 0 | 0 | **0** |
| `external_blocker` | 1 — F23 | 1 — 26 | **2** |
| `not_applicable_with_evidence` | 0 | 0 | 0 (встречается в X-матрице и DES14) |

Было 15 `verified`; стало 16. Прибавка — F03 (импортёру в интерфейсе
доказан живым HTTP предпросмотр, commit с номерами отклонённых строк и
идемпотентный повтор). F03 получил `verified` только потому, что предыдущая
причина («мастер импорта в GUI не сделан») устранена и заменена живой
проверкой, а не потому, что зелёных тестов стало больше.

Подняты: F21 и F22 — из `todo` (код написан, но не подключён / не доходит до
bundle), F12 и F29 — из `in_progress`, F13, R16, A-F01, A-F06, A-F09, A-F31,
RES03, RES05, RES10, RES12, SRC13, SRC18, POO09, DES07, DES08, DES10 — разобраны
по коду и по живому HTTP.

Понижены обоснованно, а не по недоверию: POO05 (бюджет байтов я не
подтвердил), API04 (`diagnose health` печатает ложную тревогу) и API12 (повтор
`POST /v1/keys` отдаёт секрет второй раз).

**Два статуса изменились не мной, а параллельной правкой кода в 12:01 и
12:16**, пока документ писался: F20 и дефект 14 выросли из
`implemented_unverified` в `in_progress` — их старое обоснование («объявлено,
но не сделано» / «`reputation.py` не тронут») перестало быть правдой. Оба
остаются `in_progress`, а не `verified`: результатов новых проб я не получал,
правки не закоммичены, и на них падает
`tests/test_probes_reference.py:218`.

---

## 10. Что осталось непочиненным после серии ремонтов

Список получен на сессию и проверен по коду на HEAD `ccb0954`, а не принят на
веру. Порядок — по тяжести.

### 10.1 Воспроизведённые дефекты

1. **Повторный `POST /v1/keys` с тем же `Idempotency-Key` возвращает полный
   секрет.** Воспроизведено: первый ответ `200`, `id=ee69ad8763ccbb92`,
   `secret=pwk_634cd7c2…`; повтор — тот же `id` и тот же секрет; в `api_keys`
   одна строка. Причина: `apiv1.py:2044` кладёт в кэш идемпотентности тот же
   объект `Response`, который уходит клиенту, а `IdempotencyStore.get`
   (`:791-804`) возвращает его целиком. Механизм `apikeys.carries_one_shot` /
   `without_one_shot` (`apikeys.py:559,573`) написан, но **не вызывается
   нигде**, а правка одной строки из `HANDOFF/fix-keys.md` в дерево не попала.
   Затрагивает и `POST /v1/subscriptions`, который идёт тем же путём.
2. **`diagnose health` печатает ложную тревогу.** Воспроизведено на папке
   `data`: `health: 5 checks, 5 problems` при одном реальном отказе.
   `HealthCheck.to_dict()` (`diagnostics.py:1596-1598`) отдаёт `ok`,
   `proxytool.py:4615` фильтрует по `state`. Правка: `item.get('ok') is not True`.
   `_cmd_diagnose` дополнительно не передаёт `scope=`, поэтому проверка scope
   падает по существу.

### 10.2 Не подключено к пользовательскому пути

3. **F21 (сравнение источников и поставщиков) написано, но недостижимо.**
   `compare_sources`, `compare_suppliers`, `compare_cohorts`,
   `survival_across_windows` есть в `sourcedesk.py` и покрыты 43 тестами, но
   ни CLI, ни GUI, ни API их не вызывают.
4. **F22 (меню-бар, один экземпляр, автозапуск, сон/пробуждение) не доходит до
   пользователя поставочной сборки.** `packaging/build_macos.py` и все четыре
   `.spec` не кладут Swift-helper в bundle; `ensure_tray_helper` для frozen
   честно возвращает «в сборке нет helper меню-бара». Windows и Linux не
   проверялись ни разу. Веб-интерфейс фоновые события не показывает, маршрут
   `GET /api/desktop` не добавлен.
5. **`proxy_workbench/ui/app.js` не показывает события фонового слоя** —
   `desktop-journal.jsonl` пишется и читается меню-баром, но не страницей.
6. **`client_target`/`client_binary` не задаются ниоткуда** — сигнатура
   `proxytool.py:2682` объявляет их как `None`, и единственное место, где они
   идут дальше, — `proxytool.py:2880`. Клиентская валидация sing-box против
   целевой версии фактически не выполняется; `singbox_target(None)` всегда
   даёт `unconfigured/legacy`.
7. **`sourcedesk.REQUESTED_DDL` не исполняется.** `source_feed`,
   `membership_source` и их индексы объявлены константой; в `db.py` такой
   миграции нет. `SourceDesk` работает против схемы, которой в живой базе нет.
   `source_management.py:239` запрашивает `candidate_scope_exclusion` — таблицы
   такой в `db.py` тоже нет; исключения складиваются в сайдкар
   `data/gui-scope-exclusions.json`.

### 10.3 Ограничения, вшитые в архитектуру

8. **`collect` наполняет коллекцию до прогона.** Поток «источник → проба» есть
   внутри скана (`candidates()` читает коллекцию постранично и отдаёт байты в
   конвейер), но не между загрузкой и проверкой. Docstring
   `proxytool.py:2063-2071` признаёт это прямо.
9. **`EXPENSIVE_UNTIL_N` в продукте не используется** — выбран
   `EXPENSIVE_ALL_PASSING` (`proxytool.py:2246`).
10. **Таблица `schedules` не переживает перезапуск.** `db.py:775-777`: 11
    колонок, нет `last_run_at`, `paused`, `pause_reason`, `dst_policy`,
    `catch_up`, `wake_gap_s`. Слияние слотов после сна работает внутри
    процесса, но интервальная сетка и накопленный бюджет после рестарта
    теряются.
11. **`--run-max-bytes` мной числом не подтверждён.** Он подключён в
    `chain.Budgets(max_bytes=…)`, но на фикстуре, где тело ответа не
    читается, счётчик остаётся `0` и потолок не срабатывает.
12. **`--workers` в README описан неверно** — как «128 workers» и «Raise
    `--workers`», хотя это потолок. Флагов `--max-requests`,
    `--run-max-bytes`, `--count-what` и команды `bench` в README нет вовсе.
13. **LAN из интерфейса по-прежнему не включается.** `--lan` объявлен и
    предупреждение печатается, но `lan=True` не доходит до
    `gateway.Background`, а `Bind` без `lan=True` отвергает не-loopback адрес.
14. **Привязка шлюза к пулу из интерфейса не применяется к слушателю.**
    `POST /api/gateway/config` сохраняет и валидирует `pool_id` и перезапускает
    шлюз, но `gateway.Background(...)` вызывается без
    `bind=self.gateway_binding()`.
15. **`allow_private` в `POST /v1/collections` и `PATCH` принимается и молча
    выбрасывается** — в `api.py` это имя не встречается ни разу, в таблице
    `collections` такой колонки нет.
16. **`Workbench.country_criterion` / `filter_by_country` не имеют
    вызывающих**, а схема не держит exit-модель (`hosting_basis`,
    `observations.exit_ip`, `pools.quota_basis` отсутствуют).
17. **Периодического watch пулов нет** — `grep -rn "pools.watch"` даёт 0
    вызывающих.
18. **`db.upsert_endpoint` спрашивает схему на каждый вызов.** `db.columns()`
    (`:501-502`) не кэшируется; замерено ≈7 мкс на вызов, весь
    `upsert_endpoint` ≈33 мкс.
19. **`reputation.py` — в полёте, а не «не тронут».** До 12:01 он был нетронут
    и содержал два дефекта: разворот IPv6 через `address.exploded` с двоеточиями
    и правило «любой `127.*` = listed». В 12:01 параллельная правка перевела его
    на `probes.reverse_ip` и зональные коды, и в 12:17 обе строки в файле
    отсутствуют. Правка не закоммичена и живым DNSBL-запросом не проверена, поэтому
    пункт остаётся в списке «не подтверждено», а не в списке «сломано».
21. **Переводы отстают более чем на 350 ключей.** Встроенные `messages.en`/`messages.ru` —
    1143/1217 ключей, каждый из десяти пакетов `ui/i18n/*.js` — 791;
    `t()` (`app.js:2455-2462`) отдаёт английский текст.

### 10.4 Внешние блокеры

22. **Подпись и нотаризация macOS.** Signing credentials намеренно не
    создавались; `packaging/release_manifest.py` ставит `signed` только после
    реальной проверки подписи, манифест отдаёт `"signed": false`. Это
    `external_blocker`, а не «готово».
23. **Пересборка macOS невозможна в этой среде.** `PyInstaller` в `.venv`
    отсутствует (проверено: `ModuleNotFoundError`), установка требует сети.
24. **Windows-сборка не проверялась.** Локальная машина — macOS arm64, `iscc`
    не установлен; подтвердить может только Windows-runner.
25. **Артефакты macOS отстают.** `/tmp/pw-dist/…` — версия 2.2.1 от 25.09 при
    текущей 2.3.0.

---

**Что этот документ не утверждает.** `Ran 2480 tests` показывает
согласованность дерева, а не полноту функций. Проверки этой сессии доказывают
четыре вещи: (1) дерево согласовано и не падает; (2) до 30 функций можно
дойти снаружи; (3) конвейер действительно ведёт прогон, `--max-requests` —
настоящий потолок, а три единицы `--count-what` различаются; (4) новые
маршруты интерфейса — менеджер ключей, импортёр с предпросмотром, пулы и
расписания, привязка шлюза, хранение с предпросмотром и четыре маршрута
источников — отвечают по-настоящему. Они **не** доказывают F21 в
пользовательском пути, меню-бар в собранном приложении, транспорт
аутентификации F04, клиентскую валидацию экспортов F28, Windows-поставку и
подпись. Скорость и качество живых публичных прокси не измерены и не
оценивались — таких измерений в этом документе нет.
