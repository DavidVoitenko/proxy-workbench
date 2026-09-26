# Таблица трассировки: Proxy Workbench, обязательный scope

Ревизия: ветка `integration/ultra-2026-09-25`, HEAD `408bb3e`, версия продукта
`2.3.0` (`proxy_workbench/branding.py:14`).

Источники обязательных ID:

- `docs/requirements/MASTER-PROMPT.ru.md` §3 (дефекты 1–26), §4 (F01–F28), §5 (F29), §7 (22 сквозных сценария);
- `docs/requirements/REVIEW.ru.md` §4 (R01–R20);
- `docs/requirements/audit/AUDIT_AND_ROADMAP.ru.md` (исходные 40 замечаний F01–F40);
- `docs/requirements/audit/BACKLOG.ru.md` (140 задач);
- `docs/requirements/product-research/04-feature-cards.ru.md` (28 карточек);
- `docs/requirements/product-research/03-idea-catalog.ru.md` (X01–X08 и отвергнутые R-идеи).

Чтобы не смешивать два разных пространства имён, ниже аудит finding помечен
`A-FNN`, а карточка исследования — `K-FNN`.

---

## 0. Что фактически выполнено в этой сессии приёмки

| Проверка | Команда | Результат |
| --- | --- | --- |
| Полный набор тестов | `.venv/bin/python -m unittest discover -s tests` | `Ran 2437 tests in 159.477s` / `OK (skipped=1)` / **код выхода 0** |
| Smoke-сборка | `.venv/bin/python -m pip wheel --no-deps --wheel-dir /tmp/pw-smoke-dist .` → `python -m venv /tmp/pw-smoke-venv` → `pip install` → `packaging/smoke.py /tmp/pw-smoke-venv/bin/proxy-workbench` | `Proxy Workbench 2.3.0`, `{"exit_code": 0, "passed": 1}`, `smoke test passed`, **код выхода 0** |
| Функциональная приёмка снаружи | `.venv/bin/python functional_acceptance.py` | 30 `СДЕЛАНО`, 0 `СЛОМАНО`, 0 `НЕ СДЕЛАНО`, **код выхода 0** |

Три ограничения этих прогонов, без которых нельзя читать их как доказательство
всего scope:

1. **Приёмка проверяет достижимость, а не полноту.** Она отвечает на вопрос
   «дойдёт ли человек до функции», для 30 пунктов. Она не проверяет F21, F22,
   F04-транспорт аутентификации, Windows-поставку и подпись.
2. **Один её вердикт — ложноположительный.** Пункт «Менеджер ключей в
   интерфейсе» помечен `СДЕЛАНО`, потому что регулярное выражение
   `api.?key|apiKey|ключ` (`functional_acceptance.py:311`) совпало с подстрокой
   «ключ» внутри русских слов «ис**ключ**ённые», «ис**ключ**аются»
   (`proxy_workbench/ui/app.js:1035,1153,1530`). Прямая проверка: в `gui.py` 0
   маршрутов `/api/(key|apikey)`, в `proxy_workbench/ui/` нет ни `apiKey`, ни
   `api-key`, ни `key-manager`; два совпадения по слову «API-Key» — это каталог
   поставщиков («Free with an API key», `app.js:629`), а не менеджер ключей
   Workbench. **F29 в GUI не сделан** — см. строку F29 ниже.
3. **Первый сквозной тест приёмки использует локальные заглушки.**
   `tests/test_acceptance.py::test_a_run_with_the_default_sources_produces_several_usable_proxies`
   поднимает свой HTTP-сервер со списком `11.0.0.x` и подставляет `probe`;
   скорость живых публичных прокси не измерена и по условиям не измерялась.

---

## 1. F01–F29 — функциональные направления

| ID | Что означает | Статус | Чем подтверждено |
| --- | --- | --- | --- |
| F01 | Явные режимы проверки, basic без URL, честное различие отказа цели и отказа прокси | `implemented_unverified` | Код: `proxy_workbench/probes.py:161` `COLLECT_ONLY`, `:178` карта режимов, `capability_matrix()` `:2223`. Достижимость снаружи: `collect` реально выполнен приёмкой (`functional_acceptance.py` → «Импорт собственного списка», `СДЕЛАНО`), полный путь collect→scan→admit→publish→gateway подтверждён smoke-прогоном (`{"exit_code": 0, "passed": 1}`). Не выполнено: сам сценарий «найти 5 рабочих без URL» — внешняя сеть запрещена условиями |
| F02 | Коллекции, membership, явный scope, изоляция от публичной базы | `verified` | `db.py` collections+membership, `proxytool.add_members` `:2745`. Сквозной сценарий §7.5: `tests/test_acceptance.py::test_a_personal_import_after_a_big_public_sweep_checks_only_that_collection`; дефект 11 на реальной legacy-базе — `tests/test_db_collections.py`. Своя коллекция выбирается в интерфейсе (`app.js`, 86 вхождений `collection`) |
| F03 | Импорт: файл/drag-and-drop/буфер, форматы, preview, merge/replace, отмена, идемпотентный commit | `implemented_unverified` | Код `importer.py` (1162 строки). CLI: `import preview\|commit\|list`; приёмка реально выполнила `collect --no-sources --input` с плохой строкой → `СДЕЛАНО`. Повторный commit: `test_a_repeated_import_commit_does_not_duplicate_membership`. **Не сделано: мастер импорта в GUI** — в `gui.py` нет ни одного упоминания `importer`; собственный список в UI это textarea + прямой `POST /api/collections/members`. CSV/JSON в GUI не принимаются |
| F04 | HTTP Basic / SOCKS5 auth, hostname, отдельные access identity, OS vault | `in_progress` | **Механизм есть**: `secrets.py` (`AccessStore`, `rotate` поднимает `access_revision`), проверено сквозным `test_a_password_change_revokes_the_previous_proof`. **Транспорт аутентификации не реализован**: `probes.py:589`, `proxytool.py:138,329,1107,1123` отвергают любой `username`/`password` в URL; grep по `Proxy-Authorization\|proxy_auth\|ProxyBasicAuth` даёт 0 совпадений. macOS `security` CLI сознательно не сделан (пароль в argv); Windows OS-vault — нет. Честно записано в `CHANGELOG.md` «Known gaps» |
| F05 | Именованные профили, версии, all/any/K, required/optional, fail-fast | `implemented_unverified` | Код `profiles.py`; `profiles_module.run_request` вызывается из `api.py:2212`. Пресеты и `ServiceSet` в интерфейсе. **Не сделано: переключение профиля в CLI/GUI не исполняет профиль** — `proxytool.check_proxy`/`allowed_failures` по-прежнему считают допуск по одному глобальному `min_success` |
| F06 | Каталог сервисов и наборов с точными probes и датой проверки определения | `implemented_unverified` | Код `servicecatalog.py`; `Workbench.presets` → `search_presets` (`proxytool.py:2760`); в UI 2 вхождения `service.?set`. Границы честно записаны: Steamworks API-хост официальной документацией **не подтверждён** (пример на `partner.steam-api.com`), Reddit Data API вернул 403, поэтому это homepage-пробы с раскрытием в `not_proved`. Единственная исследовательная не-homepage добавка — `microsoft-oidc-discovery` |
| F07 | Расширенные параметры, единицы, лимиты, единая валидация, никакого молчаливого игнора | `implemented_unverified` | Код `probes.py` (валидация с лимитами), `E_VALIDATION_UNKNOWN_FIELD` в `pools.py:246`, `apikeys.py:89`, `scheduler.py:155`. **Осталось — конкретный контрпример**: `allow_private` объявлен в теле `POST /v1/collections` и `PATCH` (`apiv1.py:1120,1126`) и попадает в `openapi.json`, но в `proxy_workbench/api.py` это имя **не встречается ни разу** (`grep -n "allow_private" proxy_workbench/api.py` — 0 совпадений), а в таблице `collections` такой колонки нет: параметр принимается с кодом 200 и молча выбрасывается. Доступ при этом не расширяется |
| F08 | Страны: include/exclude, endpoint vs exit, unknown policy, GeoIP, ASN | `in_progress` | Модуль `geo.py` полон (критерий, `evaluate`, `digest`, `hosting_basis`, IPv6, GeoIP status). `proxytool.py:2684` объявляет «один критерий для CLI, GUI и API», но `Workbench.country_criterion`/`filter_by_country` **не имеют ни одного вызывающего**: `grep -rn "filter_by_country\|country_criterion" --include=*.py` даёт только определения. `api.py:825-827` и `gui.py:1996-1997` фильтруют страну своей строкой `row.get('country') not in str(country).split(',')`. **Схема не держит модель**: в `db.py` нет ни `endpoints.hosting_basis`, ни `observations.exit_ip`/`exit_country`/`exit_country_source`/`exit_country_at`, ни `pools.quota_basis` (`grep -n "hosting_basis\|exit_ip\|exit_country" proxy_workbench/db.py` — 0 совпадений), поэтому «hosting — эвристика, а не доказательство» и квоты по exit-IP нечем выразить в данных. Приёмка §7.2 `test_country_by_name_and_unknown_behave_the_same_in_every_interface` зелёный, но проверяет критерий, а не равенство выдачи трёх поверхностей |
| F09 | Freshness и доказательства: единый admission, checked_at/published_at, fail не маскируется старым success | `implemented_unverified` | `core.py` (`admit`, `select`, `POLICY`); `api.py:270-315` `Exports.load` отдаёт только свежие строки, один истёкший не обнуляет набор. Сквозные тесты: `test_an_unknown_measurement_time_is_not_fresh`, `test_a_re_export_cannot_move_a_measurement_time`, `test_one_expired_row_does_not_empty_the_set`. **Ограничение**: `MAX age` по-прежнему один, доводы приёмки «поверх GUI/API/gateway/new connection одинаково» проверяются на уровне admission-контракта, сквозной путь до нового соединения я не гонял |
| F10 | Диагностика стадий, воронка, восстановительное действие | `implemented_unverified` | CLI `diagnose funnel\|zero\|control\|health\|bundle` (`proxytool.py:4114`) работает — `functional_acceptance.py` подтвердил слово `diagnose` в `--help`; `diagnostics.py` (`build_funnel`, `explain_zero`, `check_control`). **Не сделано в GUI**: `grep -c "funnel" proxy_workbench/ui/app.js` = **0**. Живой сетевой путь `check_control` я не выполнял (внешняя сеть запрещена). **Дефект, найденный прогоном**: `diagnose health` печатает ложную тревогу — `HealthCheck.to_dict()` отдаёт булево поле под ключом `ok`, а `proxytool.py:4157` считает проблемами `item.get('state') != 'ok'`; поля `state` в словаре нет, поэтому на исправной папке получается «5 checks, 5 problems» |
| F11 | Задания: персистентные состояния, очередь, pause/resume/cancel, crash recovery, идемпотентность | `verified` | `jobs.py` + `proxytool.py:2022-2065` (движок отдаёт очередь в `jobs.py`), `Workbench.jobs()` `:2633`. API `POST /v1/checks/check` вернул **HTTP 202** в функциональной приёмке. `test_acceptance.py::test_one_idempotency_key_does_not_start_a_second_run`; тесты `tests/test_jobs_*` зелёные в полном прогоне |
| F12 | Конвейер: стадии, первые результаты, find-N, backpressure, ресурсные бюджеты | `in_progress` | **Ресурсные бюджеты и три N починены**: `--max-requests`/`--run-max-bytes` начисляются, `--count-what {endpoint,ip,exit}` различает единицы. **Сам конвейер не исполняется движком**: `grep -oE "chain\.[A-Za-z_]+" proxy_workbench/proxytool.py` даёт только `Budgets`, `ResourceGate`, `SystemClock`, `Ledger`, `FindPolicy`, `BudgetExhausted`; `Pipeline`, `run_pipeline`, `AdaptiveConcurrency`, `HostLimiter` в движке не вызываются. Стадий cheap→basic→expensive в продукте нет, пер-хоста/пер-таргета лимитов нет. Бенчмарки в `pipeline.py:2409` честно помечены `SYNTHETIC_NOTICE`, но описывают цепочку, которую программа не запускает |
| F13 | Полный source manager: каталог, адаптеры, 304, last-good, карантин | `implemented_unverified` | `sourcedesk.py` (1642 строки), `Workbench.sources()` `:2669`, `collect` `:785`, `import_clash`/`import_singbox` `:2736`. Каталог `proxy_workbench/sources.json`. **Не сделано**: GUI-маршруты `recover_source`, `exclude_source_scope`, `clear_scope_exclusions`, `scope_exclusions` в `gui.py:2925-2934` — заглушки, отвечающие «ок» без работы; диалог исключений в `index.html:1950` никогда не открывается (0 вызовов `showModal`) |
| F14 | Постоянный пул: desired N, резерв, refill, cooldown, квоты | `implemented_unverified` | `pools.py`; `Workbench.pool_refill` `:2770` вызывается из `api.py:1761,1789`; `POST /v1/pools/{id}/refill` теперь наполняет **тот** пул, который в пути, и `/start` больше не отвечает 500. **Не сделано: периодический watch** — `grep -rn "pools.watch" proxy_workbench/` даёт 0 вызывающих, поэтому «пул сам восстанавливается до N со временем» снаружи не наблюдается; только по явному вызову refill. Квоты по exit-IP нечем хранить: таблица `pools` (`db.py:737-740`) несёт 10 колонок и не содержит `quota_basis` |
| F15 | Расписания, timezone/DST, quiet hours, бюджеты, уведомления | `implemented_unverified` | `scheduler.py`; `Workbench.schedules()` `:2643`, `schedule_plan` `:2776`; CLI `schedule` подтверждён приёмкой. **Не сделано дважды**: (1) `Scheduler.mark_wake()` (`scheduler.py:2117`) не имеет ни одного продуктового вызова (только `tests/test_scheduler_plans.py:272`), то есть no-catchup после сна в продукте не включается, и F22 не реализован; (2) таблица `schedules` в `db.py:744-749` несёт 11 колонок и **не содержит** `last_run_at`, `paused`, `pause_reason`, `dst_policy`, `catch_up`, `max_catch_up`, `wake_gap_s`, `power_json`, `notify_json`, `counters_json` (`grep -n "last_run_at\|dst_policy\|catch_up" proxy_workbench/db.py` — 0 совпадений), поэтому перезапуск процесса теряет интервальную сетку и накопленный бюджет |
| F16 | Gateway: binding к пулу+профилю, ротация, sticky, лимиты, transport parity | `implemented_unverified` | `gateway.py:604-637` `reserve()` — слот берётся **до** первого `await`; `Binding.pool_id` `:215`; `denylist`, `denylist отзывает admissions`, `session TTL`. **Не сделано**: `--lan` в `gui.py:2445` объявлен, но `lan` не передаётся в `gateway.Background(...)` (`:2497`, `:916`) — включить LAN из GUI нельзя; `--gateway-interface` отсутствует; `run_gateway` в CLI не имеет `--pool` |
| F17 | Законченный путь подключения: пул → приложение → поля → контроль маршрута → отключение | `implemented_unverified` | Страница шлюза `index.html:1066-1263`, recipes, QR `app.js:3577`. **Не сделано**: контроль маршрута (last_proxy/pin) в снимке пула отсутствует; **round-trip декодирование QR не проверяется** — `tests/test_qr.py` (21 строка) проверяет только наличие `class="qr-svg"`, а не декодирование обратно в исходный URL (R20 это отмечал) |
| F18 | Единое управление GUI/CLI/API, одинаковые units/codes/permissions | `implemented_unverified` | `api.py` вызывает `profiles.run_request`, `pools`, `jobs`, `scheduler`, `sourcedesk`; CLI-команды `profile`/`pool`/`schedule`/`backup`/`api-key` подтверждены приёмкой; `tests/test_profiles_parity.py` зелёный. **Не сделано**: `tests/test_parity` (CONTRACTS §2.3) не существует; равенство выдачи GUI/API/CLI на одном scope доказано только для профилей |
| F19 | Работа со списком: page/selected/all-matching, bulk, теги, saved views, выделенное не переносится на другой scope | `implemented_unverified` | 26 вхождений `bulk` в `app.js`; `proxytool.py:2343` `kind = 'selection'`, `:2516` отдельная ветка — экспорт выделенного не публикует поколение. `test_exporting_selected_rows_does_not_switch_the_active_pool`, `test_a_selection_export_over_the_api_does_not_move_the_active_pool` (зелёные). **Не сделано**: saved views, теги, заметки, матрица proxy×target — подтверждений в коде нет |
| F20 | Дополнительные измерения: WebSocket, длительность, media, self-hosted reference probe | `implemented_unverified` | `probes.py:2248` `reference_probe_targets`, `:2320` `serve_reference_probe` — self-hosted reference probe есть; `capability_matrix()` `:2223` объявляет websocket/media/UDP/HTTP2 как **не поддерживаемые с объяснением** (F20 это прямо разрешает); `INSUFFICIENT_SAMPLE` `:113`. **Не сделано**: WebSocket handshake, длительное соединение, media manifest/segment не реализованы (объявлены, а не сделаны) |
| F21 | Сравнение источников и провайдеров: cohorts, survival, overlap, стоимость пригодного адреса | `todo` | `grep -rniE "cohort\|survival\|overlap\|cost_per\|unique_contribution" --include=*.py proxy_workbench/` даёт только `geo.py:416` (пересечение include/exclude стран) и docstring `proxytool.py:2154`. Ни в одном модуле нет сравнения источников. `sourcedesk.py` (1642 строки) не содержит ни сравнения, ни cohort, ни survival. **Единственное исключение**: `proxytool.py:2154` даёт `recommended`-score с учётом «survival across re-checks» — это скоринг адреса, а не сравнение поставщиков |
| F22 | Удобная фоновая работа: tray, close vs quit, повторный запуск, autostart, sleep/wake | `todo` | `grep -rniE "tray\|меню-бар\|автозапуск\|autostart\|LaunchAgent" --include=*.py proxy_workbench/` даёт **одно** совпадение — комментарий `pools.py:500` («without a tray»). Честно записано в `docs/packaging/RELEASE-NOTES.ru.md:44-46` и в `CHANGELOG.md` «Known gaps». Точка входа `Scheduler.mark_wake()` есть, её никто не зовёт |
| F23 | Поставка Windows/macOS/Linux, per-user пути, обновления, подпись | `external_blocker` (частично `implemented_unverified`) | Сборщики и spec присутствуют (`packaging/build_macos.py`, `build_windows.py`, 4 spec, `windows-installer.iss`, `verify_release.py`). **Артефакт macOS существует**: `/tmp/pw-dist/Proxy Workbench.app` + `.dmg` + `.zip` + manifest, но версии **2.2.1 от 25.09 21:49** при текущем `2.3.0` (HEAD `408bb3e`) — устарел на десятки коммитов, пересборка и проверка на текущей ревизии не выполнены. **Внешний блокер**: `PyInstaller` в `.venv` отсутствует (`.venv/bin/python -c "import PyInstaller"` → ModuleNotFoundError), установка требует сети; подписанных артефактов нет вовсе, манифест отдаёт `"signed": false`. Windows-сборка ни разу не выполнялась (машина macOS arm64, `iscc` не установлен) |
| F24 | Данные, backup, restore, retention/cleanup с preview | `verified` | **Вторая половина F24 вынесена в CLI**: `proxytool.py:3918` `backup create\|list\|verify\|preview\|restore\|rollback\|retention\|cleanup\|rebind`, каждое изменяющее действие с `--apply`; `_cmd_backup_restore:3973`, `_cmd_backup_cleanup:4022`, `_cmd_backup_rebind:4044`. `db.py` миграции 0..14, `VACUUM INTO` backup с manifest/checksum, `restore-preview` в новый data path. Retention с политикой по умолчанию починен (порядок удаления). `clear-data` больше не идёт мимо `db.cleanup_preview` |
| F25 | Помощь и диагностика: коды, bundle с preview/redaction, версии/scope/job health, fixture recipe | `implemented_unverified` | `diagnose health\|bundle\|zero\|funnel\|control` в CLI работает (слово `diagnose` подтверждено в `--help` приёмкой); `diagnostics.build_bundle` пишет `diagnostics/bundle.json` с числом redactions (проверено запуском). **Не сделано**: в GUI нет ни кнопки диагностического пакета, ни счётчиков воронки, ни объяснения «почему 0 результатов» (`grep -c funnel app.js` = 0). **Дефект**: `diagnose health` считает проблемами `item.get('state') != 'ok'` (`proxytool.py:4157`), тогда как `HealthCheck.to_dict()` отдаёт `ok`; на исправной базе вывод «5 checks, 5 problems». Правка: фильтровать по `item.get('ok') is not True` |
| F26 | OSS, шаблоны, доступность, RU/EN, документация под реальные возможности | `implemented_unverified` | `CHANGELOG.md` `[Unreleased]` заполнен (13 Fixed + Added + Known gaps), версия поднята `2.2.1` → `2.3.0` (`branding.py:14`), README.md/README.ru.md по 10 совпадений на `api-key|/v1/`. Переводы: 8+ языков в `proxy_workbench/ui/i18n/`. **Не сделано**: `SECURITY.md` и `CONTRIBUTING.md` — 0 совпадений по `api-key|/v1/`; `tests/test_qr.py` и `test_gui_collections_catalog.py` по-прежнему проверяют исходный текст, а не поведение (запрещено MASTER-PROMPT §7) |
| F27 | URL-подписки и жизненный цикл feed | `implemented_unverified` | `sourcedesk.py` `import_subscription`/`import_clash`/`import_singbox`/`feed_diagnostics`, ETag/Last-Modified/304, last-good, redaction секретных URL. **Не сделано**: миграция 15 `source_feed`/`membership_source` не применена (db.py — чужой файл), поэтому `SourceDesk` работает против схемы из `REQUESTED_DDL` в тестах, а не против мигрированной БД; ни одного живого `fetch` я не выполнял (внешняя сеть) |
| F28 | Экспорты и согласованные snapshots | `implemented_unverified` | `exportsvc.py`; общий snapshot contract; `kind='selection'` не публикует поколение; `api.py:270-315` фиксирует поколение один раз, сверяет `_manifest_digest` и отказывает при несовпадении. **Не сделано**: `client_target`/`client_binary` не заполняются ниоткуда (ни CLI, ни GUI, ни тело `POST /v1/exports`), поэтому `singbox_target(None)` всегда даёт `unconfigured/legacy`, а `client_check()` с настоящим бинарником sing-box недостижим — R14 в этой части остаётся открытым. Независимая находка: `exportsvc.py:1380-1403` при `FileExistsError` вызывает `remove_generation()` и может стереть чужое поколение |
| F29 | Полноценное API и менеджер ключей | `in_progress` | **API и CLI проверены снаружи в этой сессии.** `functional_acceptance.py` (код выхода 0): `/v1/capabilities` отдал 26 прав; `api-key bootstrap` в CLI выдал админ-ключ; 13 операций (`collections` read/create, `profiles` read/create, `sources` + catalog, `jobs` list, `POST /v1/checks/check` → **202**, `pools`, `schedules`, `keys`, `audit`, `results` c курсором) — все HTTP 200/202. Разграничение: ключ A с `read.results` получил **403** на административной операции и **403** на попытку выдать себе ключ; ключ B с `admin.keys` — доступ; секрет не утёк в `GET /v1/keys`. **Не сделано четыре вещи**: (1) **менеджер ключей в GUI** — в `gui.py` 0 маршрутов про ключи из 49 `/api/`-маршрутов, в `proxy_workbench/ui/` нет ни одного элемента управления ключами; вердикт приёмки «Менеджер ключей в интерфейсе → СДЕЛАНО» — ложноположительный regex (см. §0, п. 2); (2) **per-key `concurrency`** — `Principal.concurrency` записывается (`apiv1.py:546`) и больше нигде не читается, единственный `ConcurrencyLimiter` (`apiv1.py:1645`) пер-серверный на 16, `QuotaGuard` (`apikeys.py:1303`) не имеет продуктового вызова; rate-limit при этом работает; (3) **резервирование** — см. API05; (4) узкий read-only subscription secret для клиентов без заголовков (вопрос в `HANDOFF/apiv1.md §5.2`). Прочие дефекты object-level scope, чувствительного экспорта и `/v1/results/{id}` исправлены и закрыты `tests/test_acceptance.py` |

---

## 2. Дефекты 1–26 (MASTER-PROMPT §3)

| ID | Что означает | Статус | Чем подтверждено |
| --- | --- | --- | --- |
| 1 | Срок годности проходит scanner → БД → GUI/API/export/gateway | `verified` | `core.apply_measurement` пишет `valid_until`; `db.py:663-665`; `test_a_re_export_cannot_move_a_measurement_time`, `test_an_unknown_measurement_time_is_not_fresh` (зелёные в прогоне `Ran 2437 tests`). Строка без записанного срока читается как `freshness=unknown`, а не как вечно свежая |
| 2 | Переэкспорт и смена watch не создают новое время измерения | `verified` | То же `test_a_re_export_cannot_move_a_measurement_time`; `Exports.load` берёт время из строки, не из `mtime`/`size` публикации |
| 3 | Истечение одного участника не скрывает остальных свежих | `verified` | `api.py:270-315`: `admitted = [row for row in rows if row['freshness'] == 'fresh']`, `expires_at = max`; `test_one_expired_row_does_not_empty_the_set` |
| 4 | Неизвестное время / future / clock rollback дают явное состояние | `verified` | `core.py` (`TIME_UNKNOWN`, `TIME_FUTURE`, `CLOCK_ROLLBACK`), `api.py:203-210` `TIME_STATE_CODES`; `test_an_unknown_measurement_time_is_not_fresh` |
| 5 | Рейтинг, детали и скачивание доступны во время scan/watch | `implemented_unverified` | Код починен: `gui.py:225-233` класс `Snapshot` читает опубликованное поколение «read without any writer lock» (docstring прямо называет дефект 5), `gui.py:523` `read_db` — «never takes the workbench lock», `data_lock` (`:999`) остался только на записи `start`. `/v1` тоже без writer-lock, `tests/test_apiv1_limits.py` зелёные. **Не выполнено мной**: сценарий «таблица и скачивание отвечают во время активного scan/waiting-watch» через GUI я не запускал, и ни один тест не инстанцирует `Snapshot` напрямую (`grep -rn "Snapshot(" tests/` — 0 совпадений) |
| 6 | Recheck/cancel/crash не уничтожает историю и очередь | `verified` | `jobs.py` (персистентные item-состояния, checkpoints, crash recovery); `tests/test_jobs_lifecycle.py`, `test_jobs_recovery.py` зелёные |
| 7 | Экспорт выделенного/top-N — отдельный артефакт, активный пул не двигается | `verified` | `proxytool.py:2343` `kind='selection'`, `:2516`; `test_exporting_selected_rows_does_not_switch_the_active_pool`, `test_a_top_n_slice_is_its_own_artifact`, `test_a_selection_export_over_the_api_does_not_move_the_active_pool` |
| 8 | Scope/profile/generation фиксированы у потребителей | `implemented_unverified` | `Exports` сверяет `schema_version` и manifest, поколение фиксируется один раз (`api.py:290-300`). Ограничение: `test_a_foreign_job_id_is_not_reachable_by_guessing` подтверждает object-level, но полный «проверка B не перенаправляет подключение A» на живом соединении я не гонял |
| 9 | Повреждённый указатель даёт одинаковое явное unavailable | `verified` | `api.py:323-327` `_refuse(..., 'E_STATE_NO_SNAPSHOT')`; `test_a_broken_pointer_is_the_same_explicit_unavailable_everywhere` |
| 10 | Hostname/private/auth — сквозной путь либо сразу отказ | `verified` | `importer.EndpointPolicy`; hostname отвергается одинаково во всех форматах и каналах, credentials отвергаются всегда. `test_a_hostname_of_your_own_passes_the_whole_path` (с подставным `probe`); `test_a_credentialed_endpoint_is_never_put_in_a_public_artifact` |
| 11 | Собственный список изолирован от старой публичной базы | `verified` | `db.py` collections/membership как разные сущности; `test_a_personal_import_after_a_big_public_sweep_checks_only_that_collection` + тест на реальной legacy-базе в `tests/test_db_collections.py` |
| 12 | Watch/refill восстанавливает пул, а не исключает навсегда | `implemented_unverified` | `pools.py` `refill`/`refill_all` + `tests/test_pools_lifecycle.py` (сценарий N=5 → два отказа → восстановление) зелёные. **Не сделано снаружи**: `grep -rn "pools.watch" proxy_workbench/` → 0 вызывающих; через CLI/API работает только явный `pool_refill` (`api.py:1789`). Периодического watch в движке нет |
| 13 | Валидация judge до elite; invalid/empty/CAPTCHA → unknown | `verified` | `probes.py:2248+` validate-before-elite; `tests/test_anonymity.py` зелёные. **Оговорка**: тест `tests/test_anonymity.py:206-210`, закрепляющий тихий сброс `min_anonymity` в `any`, остался в дереве и не переписан |
| 14 | DNSBL: IPv6 reverse, зонные коды, quota≠listed | `implemented_unverified` | `probes.py` закрыл код: dotted nibbles, `DNSBL_QUOTA`/`DNSBL_ACCESS`/`DNSBL_LISTED`/`DNSBL_CLEAR`/неизвестные зоны. **Но старая реализация жива параллельно**: `reputation.py:184` по-прежнему `return "".join(reversed(address.exploded)) + "."` (двоеточия вместо точечных нибблов), `reputation.py:237` — `listed = any(item.startswith("127.") ...)`, то есть любой `127.*` по-прежнему = listed. Владельцу `reputation.py` файл передан не был |
| 15 | Скорость: верное окно, минимальный sample, insufficient, отдельные transfer/TTFB | `verified` | `probes.py:113` `INSUFFICIENT_SAMPLE`, отдельные замеры transfer и TTFB; `tests/test_probes_*` зелёные |
| 16 | Gateway резервирует слот до await; cancel/error освобождает | `verified` | `gateway.py:604-637` `reserve()`/`_pick(reserve=True)`, освобождение в обработчике; `tests/test_gateway_reservation.py`, `test_per_proxy_limit` — зелёные |
| 17 | HTTP upstream health ≠ TCP-open; нет повторов неидемпотентных | `implemented_unverified` | `gateway.py` различает «туннель установлен» / «получен ответ» / «запрос успешен»; `tests/test_gateway_health.py` зелёные |
| 18 | Gateway/GUI/API имеют разные secrets; LAN — явный opt-in | `implemented_unverified` | **CLI починен**: `proxytool.py:3102` отдельный `--gateway-token`/`PROXY_WORKBENCH_GATEWAY_TOKEN`, `:3292-3294` отказ при совпадении с API-токеном, `compose.yml` использует обе переменные; GUI генерирует отдельный токен на запуск. **Осталось**: `gui.py:2445` объявляет `--lan`, но `lan` не передаётся в `gateway.Background` (`:2497`, `:916`) — включить LAN из GUI по-прежнему нельзя |
| 19 | Denylist применяется до prefilter и отзывает новые admissions | `implemented_unverified` | `Denylist.match()` до любой сетевой стадии, отзыв admissions; `tests/test_gateway_denylist.py` зелёные. Судьба уже открытых streams — отдельная политика, задокументирована |
| 20 | Empty pool никогда не включает DIRECT; конфиги валидируются клиентом | `implemented_unverified` | **fail-closed закрыт**: `formats.py:39,62,72` — `DIRECT` не добавляется, `REJECT` fail-closed; `test_an_empty_export_never_selects_a_direct_route` (зелёный). **Вторая половина открыта**: `client_target`/`client_binary` не заполняются ниоткуда → `singbox_target(None)` = `unconfigured/legacy`; настоящий бинарник sing-box не запускался, `client_check()` покрыт фейковыми исполняемыми файлами (exit 0 / exit 1) |
| 21 | Browser download: picker в активации, одно закрытие, cancel без ошибки, fallback | `implemented_unverified` | `app.js:5359` `downloadFile` — активация до первого `await`, `preventClose: true`, ровно один `close()`/`abort()`, `AbortError` без тоста, fallback через anchor. **Ограничение**: настоящим кликом в Chromium не воспроизведено — драйвера браузера в окружении нет; проверка выполнена под node со стабами |
| 22 | Источники: путь update, понятные имена, metadata после collect, карантин | `todo` | `branding.py` URL каталога, `country_resolver` до `collect`, `prune` с необратимым удалением, `public_source()` стирает path — **не исправлены**: файлы `branding.py`, `gui.py`, `proxytool.py` не менялись в этом цикле. В `sourcedesk.py` есть quarantine/last-good, но GUI-маршруты `recover_source`/`exclude_source_scope` — заглушки |
| 23 | Quick test подписан объёмом; full recheck отдельно | `implemented_unverified` | `/v1` quick-test требует `budget`/`timeout` в теле и отвечает 202; `tests/test_api.py` зелёные. **Не сделано в GUI**: `App.start({'action':'quick'})` без полного подписанного бюджета |
| 24 | Presets не оставляют параметры прошлого сценария; website ≠ весь сервис | `implemented_unverified` | `servicecatalog.py` versioned presets с явным составом и сбросом полей; `capability_matrix()` различает homepage/API/WebSocket/media; `not_proved` и `limitations_ru` раскрыты честно. **Не сделано**: место показа diff обновления пользователю (`ui/app.js` — не в этом цикле) |
| 25 | Live feed использует реальные события измерений | `in_progress` | Сторона `jobs.py` закрыта: реальные события пишутся в `job_event` по одному на item. **Не сделано**: живая лента в UI по-прежнему строится из хвоста `data/gui-events.jsonl`; `JobStore._emit` из `store()` в `proxytool.py` не вызывается |
| 26 | Реальная Mac-поставка, writable paths, Windows lifecycle, достоверные release notes | `external_blocker` | `docs/packaging/RELEASE-NOTES.ru.md` существует и честно перечисляет, что не сделано. **Mac-поставка материально существует** (`/tmp/pw-dist/…app/.dmg/.zip` + manifest), но версии 2.2.1 от 25.09 при текущем 2.3.0; `PyInstaller` в `.venv` нет → пересборка на текущей ревизии невозможна без сети. **Windows-сборка ни разу не выполнялась** (нет Windows, `iscc` не установлен). **Подпись — внешний блокер**: Developer ID и учётные данные нотаризации отсутствуют, манифест `"signed": false` |

---

## 3. R01–R20 (REVIEW.ru.md §4)

| ID | Статус | Чем подтверждено |
| --- | --- | --- |
| R01 Freshness не проходит весь путь | `verified` | Дефект 1 выше; `test_a_re_export_cannot_move_a_measurement_time`; отдельный тест начинается настоящим `scan()`/store, а не ручной вставкой `valid_until` |
| R02 Первый истёкший адрес опустошает snapshot | `verified` | Дефект 3; `test_one_expired_row_does_not_empty_the_set` |
| R03 Таблица и скачивание заблокированы во время scan | `implemented_unverified` | Дефект 5: `gui.py:225-233` и `:523` сняли lock с read-пути; сам сценарий «отвечает во время scan/waiting-watch» я не запускал |
| R04 Экспорт выделенного меняет общий пул | `verified` | Дефект 7; три отдельных теста |
| R05 GUI может обойти отказ snapshot-reader | `verified` | `api.py:323-327` отказ при наличии `generations/` без указателя; `test_a_broken_pointer_is_the_same_explicit_unavailable_everywhere` |
| R06 Hostname принимается формой, но отбрасывается сборщиком | `verified` | Дефект 10; `test_a_hostname_of_your_own_passes_the_whole_path` |
| R07 Watch только уменьшает пул | `implemented_unverified` | Дефект 12: refill по явному вызову есть, периодического watch нет |
| R08 Анонимность может быть ложной | `implemented_unverified` | Дефект 13; оговорка про `test_anonymity.py:206-210` |
| R09 IPv6 DNSBL и коды не исправлены | `implemented_unverified` | Дефект 14; `probes.py` починен, `reputation.py` остался в прежнем виде |
| R10 Измерение Mbps прежнее | `verified` | Дефект 15; `INSUFFICIENT_SAMPLE` |
| R11 Gateway reservation и handshake deadline | `verified` | Дефект 16; `tests/test_gateway_reservation.py` |
| R12 LAN по умолчанию и тот же секрет | `implemented_unverified` | Дефект 18: секреты разведены, LAN из GUI всё ещё недоступен |
| R13 Denylist не отзывает уже выданный пул | `implemented_unverified` | Дефект 19; отзыв admissions сделан, судьба открытых streams — отдельная policy |
| R14 sing-box нуждается в versioned validation | `in_progress` | Дефект 20: fail-closed есть, `client_target` недостижим |
| R15 Скачивание закрывает stream дважды | `implemented_unverified` | Дефект 21; проверено под стабами, не в браузере |
| R16 Источники в main улучшены лишь частично | `todo` | Совпадает с дефектом 22: `branding.py`/`gui.py`/`proxytool.py` не правились |
| R17 Smart presets и «живая лента» сильнее backend | `in_progress` | Совпадает с дефектами 24 и 25: presets починены с честным `not_proved`, живая лента — нет |
| R18 Быстрый тест — диагностический | `implemented_unverified` | Дефект 23 |
| R19 Desktop-цель не закрыта | `external_blocker` | F23/дефект 26: per-user пути сделаны (`desktop.resolve_layout` → `PROXY_WORKBENCH_DATA` → `paths.default_data` `:15`), `.app` собрана, но на версии 2.2.1; Windows не собирался; подписи нет |
| R20 Инженерная приёмка и документация отстают | `implemented_unverified` | CHANGELOG `[Unreleased]` заполнен, версия 2.3.0, README обновлены. **Осталось**: `tests/test_qr.py` проверяет текст, а не декодирование; browser E2E отсутствует; `SECURITY.md`/`CONTRIBUTING.md` не обновлены под `/v1` |

---

## 4. A-F01–A-F40 — исходные 40 замечаний аудита

Дубли с MASTER-PROMPT §3 объединены без потери требования: в графе «Откуда»
указан ID, который несёт то же требование.

| ID | Откуда | Статус |
| --- | --- | --- |
| A-F01 URL каталога из `/main/sources.json` вместо package-path | R16 / дефект 22 | `todo` |
| A-F02 `scan()` пропускает старый успех без ограничения возраста | дефект 1, 2 | `verified` |
| A-F03 `--watch` только `recheck_passing=True` | дефект 12 / R07 | `implemented_unverified` |
| A-F04 GUI читает под эксклюзивным lock | дефект 5 / R03 | `verified` (код `gui.py:225-233`, `:523`; сценарий во время scan мной не запускался) |
| A-F05 Перепроверка удаляет старые results до новых | дефект 6 | `verified` |
| A-F06 `country_resolver` копирует metadata до collect | дефект 22 | `todo` |
| A-F07 Общая таблица candidates не изолирует импорт | F02 / дефект 11 | `verified` |
| A-F08 `pending=total-checked` по всей БД | F10 (завершённость относительно задания) | `implemented_unverified` |
| A-F09 `source_quality.passed` зависит от фильтров экспорта; prune удаляет | дефект 22 | `todo` |
| A-F10 Заслуга достаётся первому загрузившемуся источнику | F21 + SRC05 | `todo` (F21 целиком) |
| A-F11 `anonymity.classify` даёт elite без подтверждения | дефект 13 | `verified` в `probes.py` |
| A-F12 `min_anonymity` тихо заменяется на `any` | дефект 13 | `implemented_unverified` (тест `test_anonymity.py:206-210` не переписан) |
| A-F13 IPv6 DNSBL reverse с двоеточиями | дефект 14 | `in_progress` (`probes.py` починен, `reputation.py:184` жив) |
| A-F14 Любой `127.*` = listed | дефект 14 | `in_progress` (`reputation.py:237` жив) |
| A-F15 Mbps считает первый chunk, таймер после | дефект 15 | `verified` |
| A-F16 Fail-fast оставляет неполные samples | CHK07 / F05 | `implemented_unverified` |
| A-F17 `test_proxies` в обход pipeline и `screen_proxy` | F01 / ENG01 | `implemented_unverified` |
| A-F18 «Рекомендуемые» используют редкость URL как гипотезу | F19 (объяснение score) | `implemented_unverified` |
| A-F19 `hosting` — regex; страна по endpoint | F08 | `in_progress` |
| A-F20 `clean` по умолчанию; presets обещают лишнее | дефект 24 | `implemented_unverified` |
| A-F21 `DIRECT` в пустом Clash/sing-box | дефект 20 | `verified` (fail-closed) |
| A-F22 `Pool.pick()` до `acquire()` | дефект 16 | `verified` |
| A-F23 Deadline покрывает первый байт, не handshake | дефект 16 | `verified` |
| A-F24 `open_tunnel` ≠ успешный HTTP-запрос | дефект 17 | `implemented_unverified` |
| A-F25 Нет общего lifecycle у sessions/cache/usage | F16 (bounded cache) | `implemented_unverified` |
| A-F26 `Exports.load` читает `ranked.json` и `status.json` раздельно | дефект 8 / 9 | `verified` |
| A-F27 GUI export не передаёт `q` и `result-hosting` | F19 / F08 | `in_progress` (единый критерий не внедрён в API/GUI) |
| A-F28 `downloadFile` закрывает stream дважды | дефект 21 | `implemented_unverified` |
| A-F29 Checker поддерживает HTTPS-to-proxy, gateway отбрасывает | F16 (transport parity) / CHK03 | `implemented_unverified` |
| A-F30 Denylist после TCP-prefilter | дефект 19 | `implemented_unverified` |
| A-F31 `public_source()` стирает path | дефект 22 | `todo` |
| A-F32 `normalize()` отбрасывает hostname/auth/private | F04 | `in_progress` (hostname — да, auth — нет) |
| A-F33 `paths.default_data()` для frozen пишет рядом с exe | F23 | `implemented_unverified` (desktop задаёт `PROXY_WORKBENCH_DATA`, но сырой frozen-CLI — нет) |
| A-F34 Тема/язык в localStorage случайного порта | UX14 | `todo` |
| A-F35 RAM не ограничена; API грузит полный JSON | ENG06/ENG07 | `implemented_unverified` (`MAX_PAGE_LIMIT=1000` для `/v1`; legacy `/proxies` сохранил `MAX_LIMIT=1_000_000`) |
| A-F36 Нет временной истории, миграций, именованных профилей | ENG03/ENG04 | `verified` (`db.py` миграции 0..14, `observations`, `profile_revision`) |
| A-F37 Нет ETag/cache, общего бюджета и deadline пагинации | SRC07/SRC09 | `implemented_unverified` (ETag/Last-Modified/304 есть; общий бюджет — частично) |
| A-F38 Нет экрана отклонённых, 13 колонок, нет `role=progressbar` | F10 / UX11 | `in_progress` (CLI-диагностика есть, GUI-экрана нет) |
| A-F39 README противоречит флагам; AGENTS/CONTRIBUTING расходятся | F26 | `implemented_unverified` |
| A-F40 Нет browser E2E и packaged-Mac проверки; фиксированные порты | R20 / F23 | `implemented_unverified` (динамические порты в smoke — да; browser E2E — нет; packaged-Mac на текущей ревизии — нет) |

---

## 5. K-F01–K-F28 — 28 принятых карточек продуктового исследования

Карточки `04-feature-cards.ru.md` несут тот же scope, что F01–F28 MASTER-PROMPT,
с добавлением приёмочных сценариев. Статус = статус соответствующего F,
плюс отдельно отмечено, закрыт ли именно приёмочный сценарий карточки.

| ID | F | Статус | Приёмочный сценарий карточки |
| --- | --- | --- | --- |
| K-F01 | F01 | `implemented_unverified` | Частично: режимы в CLI есть, «найти 5 рабочих» не гонял |
| K-F02 | F02 | `verified` | Закрыт (`test_a_personal_import_after_a_big_public_sweep_checks_only_that_collection`) |
| K-F03 | F03 | `implemented_unverified` | Закрыт на CLI (`test_a_repeated_import_commit_does_not_duplicate_membership`); в GUI недостижим |
| K-F04 | F04 | `in_progress` | Не закрыт: транспорт аутентификации отсутствует; canary-секрет доказан (`test_a_credentialed_endpoint_is_never_put_in_a_public_artifact`, `test_the_canary_secret_is_nowhere`) |
| K-F05 | F05 | `implemented_unverified` | Частично: parity профилей доказан (`tests/test_profiles_parity.py`), исполнение профиля GUI/CLI — нет |
| K-F06 | F06 | `implemented_unverified` | Частично: каталог виден в UI, diff обновления пользователю не показывается |
| K-F07 | F07 | `implemented_unverified` | Частично: единицы и лимиты есть; `allow_private` в `/v1/collections` игнорируется |
| K-F08 | F08 | `in_progress` | Не закрыт: один критерий для GUI/API/ export не внедрён |
| K-F09 | F09 | `implemented_unverified` | Закрыт на уровне admission; сквозной путь до GUI/gateway/new connection не гонял |
| K-F10 | F10 | `implemented_unverified` | Частично: CLI `diagnose zero\|funnel` объясняет; в GUI ничего нет |
| K-F11 | F11 | `verified` | Закрыт (`tests/test_jobs_*`) |
| K-F12 | F12 | `in_progress` | Не закрыт: конвейер не исполняется движком |
| K-F13 | F13 | `todo` | Не закрыт: GUI-маршруты источников — заглушки |
| K-F14 | F14 | `implemented_unverified` | Закрыт по refill; не закрыт по автономному watch |
| K-F15 | F15 | `implemented_unverified` | Закрыт по DST/sleep-слиянию/бюджетам/уведомлениям в тестах; `mark_wake` не вызывается |
| K-F16 | F16 | `implemented_unverified` | Частично: reservation/auth/лимиты; LAN из GUI недоступен |
| K-F17 | F17 | `implemented_unverified` | Не закрыт: контроль маршрута и round-trip QR |
| K-F18 | F18 | `implemented_unverified` | Закрыт на CLI+API; `tests/test_parity` отсутствует |
| K-F19 | F19 | `implemented_unverified` | Закрыт в части «выделенное не переносится на другой scope» |
| K-F20 | F20 | `implemented_unverified` | Частично: self-hosted reference probe есть; WS/media объявлены неподдерживаемыми |
| K-F21 | F21 | `todo` | Не закрыт |
| K-F22 | F22 | `todo` | Не закрыт |
| K-F23 | F23 | `external_blocker` | Не закрыт на текущей ревизии |
| K-F24 | F24 | `verified` | Закрыт (`backup preview\|restore\|rollback\|retention\|cleanup\|rebind` в CLI) |
| K-F25 | F25 | `implemented_unverified` | Частично: bundle в CLI, в GUI нет |
| K-F26 | F26 | `implemented_unverified` | Частично: CHANGELOG/README/version обновлены; SECURITY/CONTRIBUTING — нет |
| K-F27 | F27 | `implemented_unverified` | Не закрыт: миграция 15 не применена |
| K-F28 | F28 | `implemented_unverified` | Частично: snapshot contract и selection-артефакт; `client_target` недостижим |

---

## 6. 140 задач BACKLOG.ru.md

Сгруппированы по семействам; каждая задача имеет ID и статус. Задачи, несущие
требование, уже описанное выше, отсылают к нему.

### 6.1 Каталог и сбор источников (SRC01–SRC20)

| ID | Статус | Примечание |
| --- | --- | --- |
| SRC01 каталог с ID/publisher/family/parser | `implemented_unverified` | `sourcedesk.py` + `sources.json`; 55 legacy-строк мигрируются |
| SRC02 карточки источников, фильтры | `in_progress` | В CLI/API есть; в GUI — заглушки |
| SRC03 журнал fetch | `implemented_unverified` | `feed_diagnostics` в `sourcedesk`; GUI-экран отсутствует |
| SRC04 раздельные счётчики raw/valid/unique/new/passed | `implemented_unverified` | Есть в отчётах источников |
| SRC05 many-to-many provenance, first/last-seen | `implemented_unverified` | `source_map` в `db.py`; миграция 15 не применена |
| SRC06 группировка зеркал и dataset-family | `implemented_unverified` | Поле family в каталоге |
| SRC07 ETag/Last-Modified/304/cache | `implemented_unverified` | Заявлено в `sourcedesk`; живых 304 я не наблюдал |
| SRC08 per-host concurrency, Retry-After, backoff | `implemented_unverified` | Есть в сборщике |
| SRC09 общий бюджет сбора | `implemented_unverified` | `CHANGELOG`: 32 МиБ / 500 000 кандидатов на источник |
| SRC10 карантин вместо prune | `in_progress` | В `sourcedesk` есть; `proxytool.prune` не обращён |
| SRC11 обновление каталога с preview/overrides/rollback | `todo` | `api.py:1848/1871`; правки в `branding.py`/`proxytool.py` не сделаны |
| SRC12 подписанный manifest каталога | `todo` | Не реализован |
| SRC13 мастер добавления URL с предпросмотром | `todo` | GUI-мастера нет |
| SRC14 JSON/CSV adapters с картой полей | `implemented_unverified` | `sourcedesk` adapters есть |
| SRC15 структурный HTML-table parser, IPv6, BOM/CRLF | `implemented_unverified` | Адаптеры покрывают форматы; устойчивость к смене разметки не проверялась |
| SRC16 полный Geonode adapter с hints | `in_progress` | Гео после collect — A-F06, не исправлено |
| SRC17 наборы «основной/SOCKS5/расширенный/свои» | `todo` | Не реализованы |
| SRC18 адаптивный выбор по уникальному вкладу | `todo` | Часть F21, не реализована |
| SRC19 паспорт источника (homepage, terms, attribution) | `implemented_unverified` | Поля в каталоге |
| SRC20 процесс предложения новых sources | `todo` | Не реализован |

### 6.2 Собственные списки и импорт (IMP01–IMP10)

| ID | Статус | Примечание |
| --- | --- | --- |
| IMP01 именованные коллекции с отдельным составом | `verified` | F02 |
| IMP02 явный scope запуска | `verified` | F02, дефект 11 |
| IMP03 owned endpoints с hostname/credentials | `in_progress` | hostname — да; credentials — отвергаются (F04) |
| IMP04 OS secret store и `auth_ref` | `implemented_unverified` | `secrets.py` vault/session; OS-vault не проверен вживую |
| IMP05 импорт URI/host:port/CSV/JSON через mapper | `verified` | `importer.suggest_mapping`, `ColumnMapping` |
| IMP06 отчёт импорта с номерами строк | `verified` | `ImportReport`, `import_batch.report_json`, `load_report` |
| IMP07 несколько файлов и вставка из буфера с preview | `implemented_unverified` | `from_text`, `from_drop`; GUI — нет |
| IMP08 merge/replace и diff состава | `verified` | `test_a_repeated_import_commit_does_not_duplicate_membership` |
| IMP09 импорт Clash/sing-box через data-only adapters | `implemented_unverified` | `import_clash`/`import_singbox` без исполнения rules |
| IMP10 привязка локального файла с обновлением | `todo` | Не реализован |

### 6.3 Проверки и качество измерений (CHK01–CHK18)

| ID | Статус | Примечание |
| --- | --- | --- |
| CHK01 общий pipeline для CLI/GUI/scheduler | `in_progress` | F12: конвейер не исполняется движком |
| CHK02 структурные стадии и коды ошибок | `implemented_unverified` | `probes.py` + `diagnostics.py`; движок печатает `type(exc).__name__` в нескольких местах |
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
| CHK13 DNSBL zone adapters, IPv6, bounded cache | `implemented_unverified` | Дефект 14; `reputation.py` не тронут |
| CHK14 раздельные endpoint/exit геолокация, ASN | `implemented_unverified` | F08; не внедрено в API/GUI |
| CHK15 самостоятельно размещаемый echo/judge | `implemented_unverified` | `probes.serve_reference_probe` `:2320` |
| CHK16 корректный bandwidth с min-duration | `verified` | Дефект 15 |
| CHK17 WebSocket и длительное соединение | `todo` | Объявлены неподдерживаемыми в `capability_matrix` |
| CHK18 лимиты на target, обработка 429 | `implemented_unverified` | Лимиты есть; 429-политика проверена на фикстурах |

### 6.4 Списки результатов (RES01–RES15)

| ID | Статус | Примечание |
| --- | --- | --- |
| RES01 fresh/stale/failed/unknown и «проверено N минут назад» | `verified` | `test_expired_and_unknown_rows_are_reachable_by_name` |
| RES02 выбор строк и групповые операции | `implemented_unverified` | 26 вхождений `bulk` в `app.js` |
| RES03 сохранённые представления и фильтры | `todo` | Не реализованы |
| RES04 карточка прокси с происхождением | `implemented_unverified` | Есть карточка деталей; объяснение допуска по core-причине частичное |
| RES05 матрица proxy × target | `todo` | Не реализована |
| RES06 отдельный список отклонённых | `in_progress` | `diagnose zero`/`funnel` в CLI; в GUI нет |
| RES07 история latency, passes, exit-IP | `implemented_unverified` | `core.history_of`; таблица не показывает |
| RES08 объяснение рекомендации | `implemented_unverified` | `proxytool.py:2154` recommended-score |
| RES09 компактные колонки по умолчанию | `verified` | `gui.py:112-114` COMPACT_COLUMNS |
| RES10 фильтры источник/коллекция/ASN/CIDR/теги | `in_progress` | Фильтры есть, теги — нет |
| RES11 группировка по host/exit-IP без потери endpoint | `implemented_unverified` | Одна строка на адрес после миграции 15 |
| RES12 избранное, заметки, теги, pin | `todo` | Не реализовано |
| RES13 сравнение запусков | `todo` | Не реализовано |
| RES14 единый экспорт текущей выборки с preview | `implemented_unverified` | `kind='selection'` + счётчики |
| RES15 копирование в URI/host:port/tool-specific | `implemented_unverified` | Есть; пароль — только отдельным действием |

### 6.5 Пул и автоматизация (POO01–POO12)

| ID | Статус | Примечание |
| --- | --- | --- |
| POO01 «поддерживать N» с watermark | `implemented_unverified` | Refill по вызову есть; автономного цикла нет |
| POO02 повторный допуск после cooldown | `verified` | `pools.py`; `tests/test_pools_lifecycle.py` |
| POO03 раздельные расписания source refresh и recheck | `implemented_unverified` | `scheduler.py`; GUI-поверхности нет |
| POO04 очередь заданий по профилям и scope | `verified` | `jobs.py` + `WriterGate` |
| POO05 бюджеты времени/трафика/requests | `verified` | `Budgets`, `--max-requests`, `--run-max-bytes` начисляются |
| POO06 checkpoint/pause/resume без потери истории | `verified` | Дефект 6 |
| POO07 уведомления без шума | `implemented_unverified` | `scheduler.Deduplication` в тестах; каналы ОС (tray) не реализованы |
| POO08 интервал recheck по живучести | `todo` | Нет счётчика отказов в `pool_member` |
| POO09 offline/sleep/wake/network-change | `todo` | `mark_wake` не вызывается (F22) |
| POO10 объяснение недобора и preview ослабления | `implemented_unverified` | `deficit_reason`; полной разбивки `deficit_reasons[]` нет |
| POO11 закреплённые sessions strict/failover | `implemented_unverified` | `gateway.py` sticky-режимы |
| POO12 агрегаты health-событий gateway | `implemented_unverified` | Есть, но не вызывают внеочередную проверку из GUI |

### 6.6 API и подключения (API01–API12)

| ID | Статус | Примечание |
| --- | --- | --- |
| API01 `/v1`, OpenAPI, стабильные коды | `verified` | `openapi.json` генерируется из таблицы маршрутов; `functional_acceptance.py` получил HTTP 200 на 13 операциях |
| API02 общие filters/sort/max-age | `implemented_unverified` | Фильтры разные на legacy `/proxies` и `/v1` |
| API03 cursor pagination, limits, ETag | `verified` | `MAX_PAGE_LIMIT=1000`; `test_an_event_stream_answers_with_frames_and_stops_on_its_own` |
| API04 health/readiness с причинами empty/stale | `verified` | `diagnose health` + `reader_state` |
| API05 lease/acquire/release с TTL | `in_progress` | `reservations.lease/acquire/release` вызывают один `_reservation` (`api.py:2254-2270`), который читает текущий снимок и отдаёт первые N строк; `lease_id`, `ttl_s` и `state` из тела **игнорируются**, ничего не бронируется и не освобождается. Читает `body.get('count') or body.get('lease')`, честно отказывает `E_STATE_SNAPSHOT_EMPTY` на пустом наборе |
| API06 bounded client feedback | `todo` | Не реализован |
| API07 SSE/events о заданиях | `verified` | `test_an_event_stream_answers_with_frames_and_stops_on_its_own` |
| API08 подписки PAC/Clash/sing-box с ETag | `implemented_unverified` | `exportsvc`; клиентская проверка версии не достижима |
| API09 проверенные примеры curl/Python/JS | `in_progress` | Полный сценарий есть только в docstring `apiv1.py:19-33`; в README/SECURITY не перенесён |
| API10 именованные пулы/порты для приложений | `implemented_unverified` | `Binding.pool_id`; `--pool` в CLI нет |
| API11 единые upstream transports | `implemented_unverified` | capability matrix; HTTPS-to-proxy parity в gateway не завершена |
| API12 token scopes, rotation, TLS reverse proxy | `verified` | Дефект 18 починен; `test_the_legacy_token_reads_and_gains_nothing` |

### 6.7 Удобство интерфейса (UX01–UX15)

| ID | Статус | Примечание |
| --- | --- | --- |
| UX01 первый запуск без терминала | `in_progress` | Текст справки объясняет сценарии (`app.js:969-1566`); сам мастер выбора цели отсутствует |
| UX02 простой и расширенный режимы | `verified` | Переключатель в UI есть |
| UX03 именованные шаблоны с точным описанием | `implemented_unverified` | `servicecatalog` + `not_proved` |
| UX04 мастер подключения и «Проверить подключение» | `in_progress` | Recipes есть; контроль маршрута (last_proxy/pin) отсутствует |
| UX05 понятные пустые/ошибочные состояния | `implemented_unverified` | `diagnose zero` в CLI; в GUI — нет |
| UX06 structured i18n с ключами | `implemented_unverified` | `error.langPack`; 8+ языков |
| UX07 вынос локализаций | `implemented_unverified` | `ui/i18n/*.js`; проверка ключей — `tests/test_i18n.py` |
| UX08 loading/offline/reconnecting и черновики | `todo` | Не реализовано |
| UX09 клавиатурные команды | `todo` | Не реализовано |
| UX10 клавиатурная навигация и фокус | `todo` | Не реализовано |
| UX11 доступный прогресс для screen reader | `todo` | Не реализовано |
| UX12 адаптация 1366×768, 200% zoom, reduced motion | `todo` | Не реализовано |
| UX13 отмена устаревших запросов и debounce | `todo` | Не реализовано |
| UX14 сохранение темы/языка/колонок/профиля | `todo` | A-F34 не исправлен: localStorage случайного порта |
| UX15 настройки с preview, backup/reset, диагностика | `in_progress` | Backup с preview в CLI; GUI-диалога диагностики нет |

### 6.8 Desktop и установка (DES01–DES14)

| ID | Статус | Примечание |
| --- | --- | --- |
| DES01 per-user data/cache/logs и portable mode | `implemented_unverified` | `desktop.resolve_layout`; `paths.default_data:18` для сырого frozen по-прежнему рядом с exe |
| DES02 самодостаточная Mac arm64 `.app` и `.dmg` | `implemented_unverified` | Артефакт версии 2.2.1 от 25.09 существует; на текущей 2.3.0 не пересобран |
| DES03 Intel/universal2 | `external_blocker` | `build_macos.py:55-58` намеренно отказывает; PyInstaller отсутствует |
| DES04 Windows installer per-user и portable ZIP | `external_blocker` | `windows-installer.iss` написан по документации, ни разу не собран |
| DES05 native desktop window | `todo` | Тонкий desktop host поверх текущего UI; нативного окна нет |
| DES06 иконки, metadata, About, версии | `implemented_unverified` | Метаданные есть, иконка/Dock не проверены |
| DES07 tray/menu bar | `todo` | F22 не реализован |
| DES08 single instance с безопасным IPC | `todo` | Не реализован |
| DES09 lifecycle workers и завершение | `implemented_unverified` | `desktop` закрывает handlers; проверка на собранном `.app` относится к старой ревизии |
| DES10 автозапуск по явному выбору | `todo` | Не реализован |
| DES11 обновления с проверкой подписи и rollback | `implemented_unverified` | `desktop.plan_update`/`apply_update`/`rollback` — preview по умолчанию; публикующего сервера нет |
| DES12 signing и notarization в release pipeline | `external_blocker` | Ключей нет, не создавались и не покупались; манифест `"signed": false` |
| DES13 установочные проверки чистой системы | `external_blocker` | Нет Windows-машины и macOS-установки на текущей ревизии |
| DES14 системный proxy (P3) | `not_applicable_with_evidence` | `BACKLOG.ru.md:221` прямо относит системный proxy/TUN к «за пределами плана релиза»; MASTER-PROMPT §0 запрещает включать системный proxy на машине пользователя в эту реализацию |

### 6.9 Архитектура, данные и качество (ENG01–ENG14)

| ID | Статус | Примечание |
| --- | --- | --- |
| ENG01 разделить collector/checker/storage/jobs/selection/export | `verified` | 34 доменных модуля в `proxy_workbench/*.py` (без `__init__`/`__main__`); движок импортирует их (`proxytool.py:2022,2633,2639,2643,2654,2669`) |
| ENG02 типизированные модели | `implemented_unverified` | Dataclass-ы есть; полной schema-проверки нет |
| ENG03 observations и latest summaries | `verified` | `observations` (миграция 4), одна строка `results` на адрес (миграция 15) |
| ENG04 версионированные migrations, backup, restore | `verified` | `db.py` миграции 0..14, `VACUUM INTO`, restore/rollback в CLI |
| ENG05 retention и cleanup с preview | `verified` | `retention_preview`/`apply_retention`/`cleanup_preview` + `backup retention\|cleanup --apply` |
| ENG06 индексы и materialized summaries | `implemented_unverified` | Индексы миграции 14; `MAX_PAGE_LIMIT` для `/v1` |
| ENG07 ограничить RAM и число соединений | `in_progress` | Бюджеты начисляются; потолки FD/RAM в движке по-прежнему с дефолтами `fds=1, ram=0` |
| ENG08 общая schema validation | `implemented_unverified` | `E_VALIDATION_*`; `allow_private` — контрпример |
| ENG09 regression/contract suite | `verified` | `Ran 2437 tests`, `OK (skipped=1)` |
| ENG10 browser E2E + accessibility | `todo` | Не реализовано; драйвера браузера в окружении нет |
| ENG11 performance benchmarks с baseline | `implemented_unverified` | `pipeline.benchmark` (синтетика, честно помечена); живых замеров нет |
| ENG12 resource limits и trust-boundary review | `implemented_unverified` | Лимиты тела/очереди/rate в `/v1`; `test_apiv1_limits.py` зелёные |
| ENG13 CLI subcommands, JSON output, exit codes | `verified` | Приёмка реально вызывала `collect`, `api-key bootstrap`, `serve`; `test_the_cli_reaches_the_import_the_pools_the_schedules_and_the_backups` |
| ENG14 структурные логи, correlation IDs, redaction | `implemented_unverified` | `test_the_canary_secret_is_nowhere`; полный diagnostic bundle только в CLI |

### 6.10 Open source и распространение (OSS01–OSS10)

| ID | Статус | Примечание |
| --- | --- | --- |
| OSS01 README с кнопками Windows/Mac и тремя сценариями | `implemented_unverified` | README обновлены; раздельных бинарных кнопок нет |
| OSS02 единый статус beta/stable, capability matrix | `implemented_unverified` | Версия 2.3.0 единая; матрица возможностей есть в `probes.py` |
| OSS03 Roadmap с milestones и критериями | `implemented_unverified` | `06-roadmap-and-release.ru.md` — исследование, не обновлённый roadmap репозитория |
| OSS04 guide для нового source/parser | `todo` | Не реализован |
| OSS05 translation guide и синхронизация EN/RU | `in_progress` | 8+ языков; `docs/` guide нет |
| OSS06 draft release после успешных сборок | `external_blocker` | Не все сборки существуют (Windows не собирался) |
| OSS07 dependency/SBOM/checksums/provenance | `implemented_unverified` | `packaging/release_manifest.py`, `verify_release.py` — для macOS-сборки 2.2.1 |
| OSS08 публикация PyPI | `todo` | Не выполнялась (публикация вне scope) |
| OSS09 Homebrew/winget/Linux multiarch | `todo` | Не реализовано |
| OSS10 согласованные CONTRIBUTING/AGENTS/SECURITY/PRIVACY | `in_progress` | Документы есть; под `/v1` и менеджер ключей не обновлены |

---

## 7. X01–X08 и отвергнутые R-идеи (03-idea-catalog.ru.md)

Не превращаются в обязательство написать новый VPN, купить сервисы или
развернуть облако. Обязательные части remote management и self-hosted probes
вынесены в F29 и F20 соответственно.

| ID | Идея | Оценка | Статус | Чем закрыто |
| --- | --- | --- | --- | --- |
| X01 | Browser extension | Не реализовывалась: расширение добавляет разрешения магазина и политику браузера, а F17 закрывается существующими страницами, recipes и QR | `not_applicable_with_evidence` | `03-idea-catalog.ru.md:75` — продолжение только если пользователи не завершают F17; F17 частично не закрыт (контроль маршрута), это известная граница, а не обязательство написать extension |
| X02 | Удалённое управление | **Обязательная часть = F29**: `--lan`/host, отдельные identity, TLS/reverse-proxy документированы, audit log, revocation/expiry. Обязательная часть выполнена на API-стороне | `implemented_unverified` | `functional_acceptance.py` подтвердил работу `/v1` с реальным ключом; LAN из GUI всё ещё недоступен (дефект 18) |
| X03 | Мобильный web-доступ к управлению | Не реализовывался: адаптивный UI для одного узла — это control plane, а не проксирование трафика телефона | `not_applicable_with_evidence` | `03-idea-catalog.ru.md:77` — native app не нужен до проверки; обязательной части в F01–F29 нет |
| X04 | Дополнительный транспорт | HTTPS-to-proxy поддержан checker'ом; gateway parity не завершена. Объём ограничен одним транспортом, добавление — по подтверждённому импорту | `in_progress` | `probes.capability_matrix()` фиксирует статус каждого транспорта; A-F29 |
| X05 | Самостоятельные проверочные endpoints | **Обязательная часть = F20**: self-hosted reference probe реализован | `verified` | `probes.reference_probe_targets:2248`, `probes.serve_reference_probe:2320`; локальные positive/negative сценарии в тестах `probes` |
| X06 | VPN/TUN-адаптер внешнего движка | Не реализовывался: XL+, требует прав, DNS, routes, исключений, UDP и отдельного продукта | `not_applicable_with_evidence` | `03-idea-catalog.ru.md:80` — экспорт в существующие клиенты дешевле; `BACKLOG.ru.md:221` относит VPN/TUN за пределы плана релиза |
| X07 | Синхронизация конфигураций | Не реализовывалась: обязательное облако отклонено, аккаунт вне scope | `not_applicable_with_evidence` | `03-idea-catalog.ru.md:81`; F24 даёт несекретный backup/restore как эквивалентный закрытый сценарий переноса |
| X08 | Поставщик с управляемыми session/rotation API | Не реализовывался: нет выбранного пользователями поставщика, покупка запрещена | `not_applicable_with_evidence` | `03-idea-catalog.ru.md:82`; эквивалентный закрытый сценарий — F27 `import_subscription` с secret-reference и redaction |
| R01 (идея) | Собственный VPN/TUN стек | Отклонена как отдельный сетевой продукт | `not_applicable_with_evidence` | `03-idea-catalog.ru.md:88` |
| R02 (идея) | Обязательный аккаунт/облако/телеметрия | Отклонена: противоречит локальному OSS | `not_applicable_with_evidence` | `03-idea-catalog.ru.md:89`; `PRIVACY.md` фиксирует отсутствие телеметрии |
| R03 (идея) | Native мобильный клиент с проксированием телефона | Отклонена: два новых OS, другой сценарий | `not_applicable_with_evidence` | `03-idea-catalog.ru.md:90` |

---

## 8. Сквозная приёмка §7 (22 сценария)

| № | Статус | Чем подтверждено |
| --- | --- | --- |
| 1 новичок без URL находит базово пригодные | `implemented_unverified` | Путь доказан на локальных заглушках (`test_acceptance.py` первый тест) и smoke-прогоном; живые публичные прокси не измерялись |
| 2 страна по имени, endpoint/exit, unknown одинаково | `implemented_unverified` | `test_country_by_name_and_unknown_behave_the_same_in_every_interface`; API/GUI фильтруют страну своей строкой (F08) |
| 3 all/any/K даёт раздельные результаты | `verified` | `test_all_any_and_k_services_give_different_results` |
| 4 изменение профиля не смешивает версии измерений | `implemented_unverified` | `profile_revision` в `observations`; сквозного прогона через GUI не делал |
| 5 свой импорт после публичного сбора | `verified` | `test_a_personal_import_after_a_big_public_sweep_checks_only_that_collection` |
| 6 CSV/JSON/TXT preview, errors, duplicates, cancel, replace, повтор | `implemented_unverified` | `test_a_repeated_import_commit_does_not_duplicate_membership`; GUI-импортёр недостижим |
| 7 hostname/auth полный путь, смена пароля | `in_progress` | `test_a_hostname_of_your_own_passes_the_whole_path` (с подставным probe), `test_a_password_change_revokes_the_previous_proof`; транспорт аутентификации отсутствует |
| 8 источники 200/304/429/partial, bad update не стирает last-good | `implemented_unverified` | Проверено на подставных `FeedResult`; живого fetch не было |
| 9 большой источник ограничен, первые результаты, stop/resume | `implemented_unverified` | Бюджеты есть; поток источник→проба в движке отсутствует (F12) |
| 10 recheck/cancel/crash, timestamps, mixed-age, corrupt pointer | `verified` | Дефекты 2, 3, 4, 6, 9; соответствующие тесты зелёные |
| 11 экспорт выбранных не переключает активный пул | `verified` | Дефект 7; три теста |
| 12 empty/unsupported/expired не выбирает DIRECT | `implemented_unverified` | `test_an_empty_export_never_selects_a_direct_route`; клиентская валидация версии недостижима |
| 13 Maintained N восстанавливается, нехватка объясняется | `implemented_unverified` | `tests/test_pools_lifecycle.py`; автономного watch нет |
| 14 gateway: concurrent max-per-proxy, partial handshake, cancel, shutdown | `verified` | `tests/test_gateway_reservation.py`, `test_gateway_bind.py`, `test_gateway_health.py` |
| 15 read-only и operator keys, scopes, revocation, SSE, quotas | `verified` | `functional_acceptance.py` раздел «РАЗГРАНИЧЕНИЕ ДОСТУПА» (все 4 пункта СДЕЛАНО) + `tests/test_acceptance.py` 10 тестов по ключам |
| 16 полный API-сценарий внешним клиентом без GUI | `verified` | `test_one_bootstrap_key_finishes_the_whole_journey_without_the_gui`; функциональная приёмка прошла целиком через CLI+HTTP |
| 17 QR декодируется, recipes проверяемы, роли различаются | `in_progress` | Рецепты есть; round-trip декодирование QR не проверяется (R20) |
| 18 Windows/Mac запуск без Python, ресурсы, второй запуск, tray | `external_blocker` | F23/дефект 26 |
| 19 сон/смена сети/vault не повреждают историю | `todo` | `mark_wake` не вызывается; F22 не реализован |
| 20 backup/migration/rollback/retention безопасны, канарейки не текут | `verified` | F24; `test_the_canary_secret_is_nowhere`, `test_a_credentialed_endpoint_is_never_put_in_a_public_artifact` |
| 21 RU/EN, клавиатура, screen reader, 200% zoom, браузеры | `in_progress` | Переводы есть; клавиатура/screen reader/zoom не проверялись |
| 22 35 сценариев исследования сопоставлены | `verified` | Раздел 5 настоящего файла: 28 карточек сопоставлены 1:1, X01–X08 и отвергнутые идеи — раздел 7 |

---

## 9. Сводка

| Статус | F01–F29 | Дефекты 1–26 | Всего (55) |
| --- | --- | --- | --- |
| `verified` | 3 (F02, F11, F24) | 12 (1, 2, 3, 4, 6, 7, 9, 10, 11, 13, 15, 16) | 15 |
| `implemented_unverified` | 19 | 11 | 30 |
| `in_progress` | 4 (F04, F08, F12, F29) | 1 (25) | 5 |
| `todo` | 2 (F21, F22) | 1 (22) | 3 |
| `external_blocker` | 1 (F23) | 1 (26) | 2 |
| `not_applicable_with_evidence` | 0 | 0 | 0 (встречается в X-матрице и DES14) |

**Что этот отчёт не утверждает.** Число прогнатых тестов не является
доказательством функции. Три проверки этой сессии доказывают: (1) дерево
согласованно и не падает, (2) установленный wheel проходит сквозной
collect→scan→publish→API→gateway на локальном mock, (3) до 30 функций можно
дойти снаружи. Они не доказывают F21, F22, транспорт аутентификации F04,
GUI-менеджер ключей F29, поставочные артефакты на текущей ревизии и подпись.
