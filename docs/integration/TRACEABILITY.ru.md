# Таблица трассировки: Proxy Workbench, обязательный scope

Ревизия: ветка `integration/ultra-2026-09-25`, HEAD `d9056e5`, версия продукта
`2.3.0` (`proxy_workbench/branding.py:14`).

Документ переписан **второй раз**, после большой волны ремонтов, которая прошла
между двумя правками этого файла: `8f9c79b`, `8e24d23`, `d9056e5`, а до них
`5c508b9` (интеграция движка и API в модули), `d917d91` (веб-подключение шлюза,
пулов, источников, диагностики, заданий, фонового слоя и десяти языковых
пакетов), `4349b53` (аутентификация к апстриму и живой `--lan` в шлюзу),
`33cec72` (схема БД 15→18), `8be591b` и `9713ee2` (поставка с меню-баром в
`.app` и точка входа до фонового слоя). Все статусы ниже пересчитаны по
фактическому состоянию дерева.

**Снимок состояния: 26.09.2026, 15:10–15:40, HEAD `d9056e5`, рабочее дерево
чистое** (`git status --short` пуст). Параллельных правок в этот раз не было:
всё, что я проверял, лежит в коммитах.

**Главное, что этот документ обязан сказать.** Между двумя правками в
`proxy_workbench/ui/app.js` была настоящая поломка: переменная
`closeDetailsBtn` использовалась на верхнем уровне и нигде не была объявлена,
поэтому файл падал при загрузке и **ни один обработчик во всём приложении не
прикреплялся**. Разметка рисовалась, сервер отвечал, 3245 тестов и 110 живых
API-проверок проходили — весь этот контроль бьёт по HTTP-стороне, которая как
раз продолжала работать. Дефект нашли открытием страницы в браузере и кликом.
Рядом с ним `setupCatalogListeners()` была определена, но не вызвана, поэтому
все кнопки каталога источников были мертвы. Оба починены (`8e24d23`,
`d9056e5`) и закрыты `tests/test_web_bundle_init.py`.

Отсюда вывод, который я провожу через весь документ: **зелёный прогон тестов и
живые API-вызовы не являются доказательством того, что интерфейс работает.**
Сторона HTTP и сторона браузера проверяются разными инструментами, и до
`8e24d23` инструмента для второй просто не было. Новый тест — первый.

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
воспроизводил, в графе «чем подтверждено» не пишется. **Прогон, который бьёт
только по HTTP, не является доказательством и для интерфейса** — см. блок про
`closeDetailsBtn` в шапке.

---

## 0. Что проверено в этой сессии

| Проверка | Команда / сценарий | Результат |
| --- | --- | --- |
| Полный набор тестов | `.venv/bin/python -m unittest discover -s tests` | `Ran 3249 tests in 231.391s` / `OK (skipped=1)` / **код выхода 0** |
| Функциональная приёмка снаружи | `.venv/bin/python functional_acceptance.py` | 30 `done`, **код выхода 0** |
| Новый тест инициализации интерфейса | `.venv/bin/python -m unittest tests.test_web_bundle_init -v` | `Ran 4 tests`, `OK` |
| Тот же тест при возврате бага | копия дерева в `/tmp`, из `app.js` убрана строка объявления и убран вызов `setupCatalogListeners()` | `FAILED (failures=2)` и `FAILED (failures=1)`; после возврата — `OK` |
| `diagnose health` | `.venv/bin/python -m proxy_workbench diagnose health --data data` | `health: 5 checks, 1 problems` — ложная тревога ушла (было `5 checks, 5 problems`) |
| Повтор `POST /v1/keys` с тем же `Idempotency-Key` | поднятый `python -m proxy_workbench serve`, первый ответ и повтор с тем же ключом | 200, **тот же `id`, секрета во втором ответе нет**, `secret_already_shown: true`, в `api_keys` одна строка |
| Диагностика в интерфейсе | HTTP к поднятому `gui.make_server`: `GET /api/diagnostics`, `POST /api/diagnostics/bundle` | 200 с воронкой, потерями, кодами, `health` (5 проверок) и текстом пакета на 1183 символа |
| Фоновый слой в интерфейсе | `GET /api/desktop` | 200: `instance`, `journal`, `autostart`, `power`, `network`, `layout`, `frozen` |
| Паритет переводов | разбор `messages.en`/`messages.ru` и всех десяти `ui/i18n/*.js` | en 1275 уникальных ключей, ru 1275, симметрическая разница **0**; каждый пакет 1275, разница с en **0** |
| Схема БД | `PRAGMA user_version` и `sqlite_master` на `data/proxies.sqlite3` | `user_version = 18`; `source_feed`, `membership_source`, `candidate_scope_exclusion` существуют |
| `singbox_target` | вызов на `None`, `1.11.0`, `1.10.0`, `latest`, `1.99.0`, `nonsense` | `unconfigured` / `configured` / `configured` / `configured` / `unverified` / `unknown` — цель достижима из CLI |
| Мелкий текст в CSS | разбор всех правил `style.css` | 551 объявление `font-size`, все в `px`, **`rem` — ноль**, пользовательской настройки размера текста нет; 1×8.5px, 6×9px, 13×9.5px, 41×10px, 42×10.5px |
| Windows-job в CI | `gh run list --workflow windows.yml` | `HTTP 404: workflow windows.yml not found on the default branch` — job `windows-artifact` (`windows-2022`) есть только в этой ветке и **ни разу не выполнялся** |

Все сетевые проверки сделаны на loopback: локальный HTTP-прокси и локальная
цель. Публичные прокси, сторонние сервисы и живые каталоги источников не
запрашивались, поэтому **скорость и качество живых публичных прокси по-прежнему
никем не измерены**, и в этом документе нет ни одной цифры о них.

**Чего я в этот раз не смог.** Проверку глазами в браузере я **не повторял**:
автоматизация браузера в этой сессии недоступна (инструмент отвечает «Browser is
not available in subagent»). Всё, что ниже сказано о внешнем виде, взято из кода
(`style.css`, `index.html`, `app.js`) и из отчёта владельца ремонта, а не из
снимков экрана, которые я снял бы сам. Это ограничение я не прячу.

### 0.1 Живой прогон (F12) — локальный mock, четыре кандидата

**Этот подраздел я не перегонял в эту сессию** и помечаю его как данные
предыдущей сессии: строки относятся к `proxytool.py` и `pipeline.py`, которых
волна ремонтов не трогала (`git show --stat` по всем коммитам волны этих файлов
не касается). Приводится как есть, с датой снятия.

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
(`gui.py:4063,4069,4071`, `3942`), делают настоящую работу. Это данные
предыдущей сессии; в эту сессию я их не перегонял и опираюсь на то, что
`gui.py` в волне ремонтов менялся (`d917d91`), а на схему БД опираюсь на
собственную проверку ниже.

**Что изменилось в этом месте и что я проверил сам.** Прежняя строка «таблицы
`candidate_scope_exclusion` в `db.py` нет» **больше не верна**: `33cec72` завёл
схему 15→18, и в `db.py:1189, 1219, 1315` теперь создаются `source_feed`,
`membership_source` и `candidate_scope_exclusion`. На живой базе
`data/proxies.sqlite3` `PRAGMA user_version` = **18**, и все три таблицы есть в
`sqlite_master`. Сайдкар `data/gui-scope-exclusions.json` (`gui.py:473`) в коде
остался, но таблица, которую он заменял, теперь существует.

### 0.3 Бывшие дефекты: что я воспроизводил и что теперь стало

Два из трёх дефектов прошлой сессии **починены в коде и проверены заново, с
обеих сторон**. Один остался.

| Дефект | Как было (предыдущая сессия) | Как стало сейчас | Чем подтверждено |
| --- | --- | --- | --- |
| Повтор `POST /v1/keys` с тем же `Idempotency-Key` отдаёт полный секрет дважды | первый ответ `200`, `id=ee69ad8763ccbb92`, `secret=pwk_634cd7c2…`; повтор — тот же `id` и **тот же** секрет | **починено**: повтор отдаёт тот же `id` и **секрета в теле нет**, `secret_already_shown: true`; в `api_keys` одна строка | Живой HTTP к поднятому `python -m proxy_workbench serve`. Код: `apiv1.py:2061` кладёт в кэш идемпотентности `apikeys.without_one_shot_response(response)`, а не сам `Response`. Механизм, который прошлый отчёт назвал «написан и не вызывается нигде», теперь вызывается |
| `diagnose health` печатает ложную тревогу | `health: 5 checks, 5 problems` при `health_report(...).failures` = 1 | **починено**: `health: 5 checks, 1 problems` | Живой прогон `.venv/bin/python -m proxy_workbench diagnose health --data data`. Поле `state`, которого нет в словаре, больше не используется как признак проблемы |
| `db.upsert_endpoint` спрашивает схему на каждый вызов | 5000 вызовов → `columns()` 7 мкс/вызов, весь `upsert_endpoint` 33 мкс/вызов | **жив, без изменений** | Код на HEAD `d9056e5`: `db.py:507-508` `columns()` по-прежнему делает `PRAGMA table_info` на каждый вызов, `db.py:2221` `upsert_endpoint` по-прежнему вызывает её на каждом вызове. Кэша (`lru_cache`, словаря, версии схемы) в файле нет — `grep -n "lru_cache\|_columns_cache\|COLUMN_CACHE" proxy_workbench/db.py` даёт 0 совпадений |

### 0.4 Падающего теста больше нет

Предыдущая сессия писала этот отчёт с оговоркой, что на дереве есть падающий
тест и полного зелёного прогона она не делала. Обе оговорки сняты:

```
$ .venv/bin/python -m unittest discover -s tests
Ran 3249 tests in 231.391s
OK (skipped=1)
```

`tests/test_probes_reference.py:218` больше не требует `supported == False` для
WebSocket, длительного соединения и media. На его месте стоит
`test_measured_capabilities_name_their_endpoint_budget_and_outcome`
(`tests/test_probes_reference.py:218-232`), который утверждает обратное и
требует, чтобы измеренная возможность называла свой эндпоинт, бюджет и
результат. Это не «тест подогнан под код»: прежнее утверждение было верно, когда
продукт умел только GET, и стало ложным, когда F20 принёс реальные пробы.
Docstring нового теста говорит об этом прямо.

**Отдельно про новый тест инициализации интерфейса.** `8e24d23` и `d9056e5`
закрыли поломку, которая не ломала ничего, что умеет проверять `unittest`, и
поэтому прошла незамеченной. `tests/test_web_bundle_init.py` вычисляет настоящий
`app.js` под DOM-заглушкой в node и падает, если скрипт бросает исключение при
загрузке, плюс проверяет, что каждая `setup*Listeners()` реально вызвана и что
`closeDetailsBtn` объявлен до использования. Я проверил его в обе стороны на
копии дерева в `/tmp`:

| Возвращённый баг | Результат |
| --- | --- |
| убрана строка `const closeDetailsBtn = $('close-details');` | `FAILED (failures=2)` |
| убран вызов `try { setupCatalogListeners(); } …` | `FAILED (failures=1)` |
| обе строки на месте | `OK` |

Это единственный тест в репозитории, который смотрит на интерфейс, а не на
HTTP-сторону. Его появление меняет то, что я могу утверждать о `app.js`: до него
я знал, что сервер отвечает, и не знал, что страница вообще запускается.


---

## 1. F01–F29 — функциональные направления

| ID | Что означает | Статус | Чем подтверждено |
| --- | --- | --- | --- |
| F01 | Явные режимы проверки, basic без URL, честное различие отказа цели и отказа прокси | `implemented_unverified` | Код: `probes.py:161` `COLLECT_ONLY`, `:178` карта режимов, `capability_matrix()` `:2223`. Живой прогон §0.1 шёл с явным `--url`. Не выполнено: сценарий «найти 5 рабочих без URL» по живой сети — внешняя сеть запрещена условиями |
| F02 | Коллекции, membership, явный scope, изоляция от публичной базы | `verified` | `db.py` collections+membership, `proxytool.add_members`. Сквозной сценарий §7.5: `tests/test_acceptance.py::test_a_personal_import_after_a_big_public_sweep_checks_only_that_collection`; дефект 11 на реальной legacy-базе — `tests/test_db_collections.py`. Импорт в свою коллекцию через интерфейс проверен: `POST /api/import/commit` → `collection_id: public-base`, `members` |
| F03 | Импорт: файл/буфер, форматы, preview, merge/replace, отмена, идемпотентный commit | `verified` | Живой HTTP §0.2: `POST /api/import/preview` → 200 с `batch_id`/разбором колонок, `POST /api/import/commit` → 200 с номерами отклонённых строк и кодами, повтор → `replayed: true`. CLI `import preview\|commit\|list` тоже работает. **Ограничение, которое я не проверял:** drag-and-drop в браузере (драйвера нет), формат `json`, режим `replace` — код написан, но эти три варианта я не гонял |
| F04 | HTTP Basic / SOCKS5 auth, hostname, отдельные access identity, OS vault | `implemented_unverified` **(поднят с `in_progress`)** | **Транспорт аутентификации теперь есть — в шлюзе.** `4349b53` и код: `gateway.py:293` `AccessCredentials` (достаёт учётные данные из `secrets.AccessStore` через `secrets.transport_credentials` и снимает read-бит, чтобы они жили только на время рукопожатия), `gateway.py:1000-1007` `Proxy-Authorization: Basic …` по RFC 7617, `secrets.py:813` `proxy_authorization`, `secrets.py:916` — пароль после ротации отправляется тот, который текущий, а не тот, который был при замере. Транспорт выбирается по схеме: HTTP/HTTPS — заголовок, SOCKS5/SOCKS5h — `username/password` в ответе, открытый прокси — без аутентификации, SOCKS4 — без неё и без попытки. 13 тестов в `tests/test_area_gateway_credentials.py`. **Чего это не доказывает:** (1) ни одного живого соединения к прокси с логином и паролем я не делал — доказательства только локальные фикстуры; (2) **измеряющая сторона по-прежнему отказывает**: `probes.py:710`, `proxytool.py:138, 333` по-прежнему отвергают любой `username`/`password` в URL, то есть end-to-end путь «привезти свой прокси с паролем и измерить его» не открыт; (3) **OS vault не проверен**: `keyring` в `.venv` отсутствует (`ModuleNotFoundError`), `secrets.py:396-426` честно отказывает и просит ставить необязательную зависимость или использовать session-секрет. |
| F05 | Именованные профили, версии, all/any/K, required/optional, fail-fast | `implemented_unverified` | Код `profiles.py`; `profiles_module.run_request` вызывается из `api.py:2212`. Пресеты и `ServiceSet` в интерфейсе. **Не сделано: переключение профиля в CLI/GUI не исполняет профиль** — `proxytool.check_proxy`/`allowed_failures` по-прежнему считают допуск по одному глобальному `min_success` |
| F06 | Каталог сервисов и наборов с точными probes и датой проверки определения | `implemented_unverified` | Код `servicecatalog.py`; `Workbench.presets` → `search_presets`. В UI 2 вхождения `service.?set`. Границы честно записаны: Steamworks API-хост официальной документацией **не подтверждён**, Reddit Data API вернул 403, поэтому это homepage-пробы с раскрытием в `not_proved` |
| F07 | Расширенные параметры, единицы, лимиты, единая валидация, никакого молчаливого игнора | `implemented_unverified` **(статус прежний, но состав причины изменился)** | **Половина контрпримера закрыта, половина осталась.** Код `probes.py` (валидация с лимитами), `E_VALIDATION_UNKNOWN_FIELD` в `pools.py:246`, `apikeys.py:89`, `scheduler.py:155`. Закрыто: `allow_private_endpoints` — это теперь настоящая, записываемая в аудит политика импорта, `api.py:1523-1545` `_import_policy` строит `importer.EndpointPolicy(public_only=not wanted)`, а `api.py:1547-1557` `_audit_import_policy` пишет её в журнал от имени ключа; живой HTTP: `POST /v1/collections` с `allow_private_endpoints` → **400 `E_VALIDATION_UNKNOWN_FIELD`**, то есть неизвестное поле больше не глотается. **Осталось: `allow_private` в теле `POST /v1/collections` и `PATCH`** (`apiv1.py:1179, 1185` — это имя объявлено в OpenAPI и попадает в `openapi.json`), живой HTTP: `POST /v1/collections {"name":"ap1","allow_private":True}` → **200**, а в ответе `{"id","name","kind","created_at"}` поля `allow_private` нет и `scope` нет. Параметр принимается и молча выбрасывается — ровно тот же контрпример, что и до волны. |
| F08 | Страны: include/exclude, endpoint vs exit, unknown policy, GeoIP, ASN | `in_progress` | Модуль `geo.py` полон. `proxytool.py:2615` `country_criterion` и `:2658` `geo_country_verdict` вызываются внутренне, но `Workbench.country_criterion` (`:3099`) и `Workbench.filter_by_country` (`:3112`) не имеют ни одного вызывающего: `grep -rn "filter_by_country" proxy_workbench/*.py` даёт только определение. `api.py` и `gui.py` фильтруют страну своей строкой. **Схема не держит модель**: в `db.py` нет ни `endpoints.hosting_basis`, ни `observations.exit_ip`/`exit_country`, ни `pools.quota_basis` (`grep -n "hosting_basis\|exit_ip\|exit_country" proxy_workbench/db.py` — 0 совпадений) |
| F09 | Freshness и доказательства: единый admission, checked_at/published_at, fail не маскируется старым success | `implemented_unverified` | `core.py` (`admit`, `select`, `POLICY`); `api.py:270-315` `Exports.load` отдаёт только свежие строки. Сквозные тесты: `test_an_unknown_measurement_time_is_not_fresh`, `test_a_re_export_cannot_move_a_measurement_time`, `test_one_expired_row_does_not_empty_the_set`. **Ограничение**: сквозной путь до нового соединения я не гонял |
| F10 | Диагностика стадий, воронка, восстановительное действие | `implemented_unverified` **(статус прежний, доказательство новое)** | **Воронка теперь в интерфейсе, а не только в CLI.** `gui.py:4095` `diagnostics_view` собирает `diag.build_funnel` + `diag.explain_zero` + потери с действиями, `gui.py:4038-4093`, маршрут `GET /api/diagnostics` (`gui.py:4532`), карточка `#diagnostics-card` (`index.html:2423-2489`), `app.js:9757-9830`. Живой HTTP на поднятом `gui.make_server`: `GET /api/diagnostics` → **200** с `stages`, `terminal`, `verdicts`, `attribution`, `totals`, `set_state`, `stop_reason`, `entry`, `lost`, `zero`, `zero_text`, `losses`, `codes`, `rows_total`, `health`, `environment`. Прежнее «`grep -c funnel app.js` = 0» больше не верно. **Чего не хватает до `verified`:** на пустой базе `stages` пуст (`rows_total: 0`) и `zero.code` = `JOB_FAILED`, то есть счётчики воронки на реальном прогоне я не наблюдал; кнопку восстановительного действия в GUI я не нажимал. `diagnose health` больше не печатает ложную тревогу (см. §0.3). |
| F11 | Задания: персистентные состояния, очередь, pause/resume/cancel, crash recovery, идемпотентность | `verified` | `jobs.py` + `proxytool.py:2022-2065`, `Workbench.jobs()`. Живой прогон §0.1 выдал `job-…` и отработал без потери результата. `POST /v1/checks/check` вернул **HTTP 202** в функциональной приёмке. `test_acceptance.py::test_one_idempotency_key_does_not_start_a_second_run`; тесты `tests/test_jobs_*` зелёные |
| F12 | Конвейер: стадии, первые результаты, find-N, backpressure, ресурсные бюджеты | `implemented_unverified` **(статус прежний, один пункт «что осталось» снят)** | Данные предыдущей сессии §0.1 (файлы волной не менялись). **Снято:** пункт «`EXPENSIVE_UNTIL_N` не используется и обоснование не записано» — обоснование теперь записано в коде, `proxytool.py:3382-3402`, восемнадцать строк комментария: `EXPENSIVE_UNTIL_N` останачивает оплату judge и загрузки тела, как только набрано N, а это правильно для прогона с единственной целью «дай мне N» и неправильно для этого, где `--min-anonymity` — это фильтр, а не цель: строка, пропустившая дорогую стадию, не имеет вердикта об анонимности вовсе, `core.Policy` ранжирует её как `unknown` и выбрасывает из экспорта, и корпус из 4000 адресов с `--want 5` дал бы 5 elite и 3995 молчаливых отказов, причём фильтр выглядел бы так, будто он отверг 3995. Цена реальна — по одному judge-запросу и одной загрузке на каждый прошедший адрес — и именно для неё существуют `--max-requests`, `--deadline`, `--judge-concurrency` и бюджеты по целям. **Осталось:** (1) поток «источник → проба» есть **внутри** скана, но не между `collect` и проверкой — docstring `proxytool.py:2063-2071` признаёт это прямо; (2) `--run-max-bytes` числом не подтверждён (§0.1) |
| F13 | Полный source manager: каталог, адаптеры, 304, last-good, карантин | `implemented_unverified` **(статус прежний, главная причина снята)** | **`REQUESTED_DDL` теперь исполняется.** Прежняя строка «`sourcedesk.REQUESTED_DDL` объявлен константой, а в `db.py` такой миграции нет, поэтому `SourceDesk` работает против схемы, которой нет» **больше не верна**: `33cec72` перенёс DDL в `db.py:1189` (`source_feed`), `db.py:1219` (`membership_source`), `db.py:1236-1242` (индексы) и добавил `db.py:1315` (`candidate_scope_exclusion`) с индексами `:1327-1330`. Своя проверка на живой базе: `PRAGMA user_version` = **18**, все три таблицы в `sqlite_master`. Тесты `tests/test_area_ddl_migration.py`, `test_area_ddl_runtime.py`, `test_area_ddl_sourcedesk.py` зелёные в полном прогоне. `sourcedesk.py`, `Workbench.sources()`, `collect`, `import_clash`/`import_singbox` — на месте; четыре маршрута источников делают настоящую работу (данные предыдущей сессии, §0.2). **Что осталось:** ни одного живого `fetch`, ни одного 304, ни одного last-good я не наблюдал — это доказательства на подставных `FeedResult`; карантин/last-good есть в `sourcedesk`, но экрана карантина в интерфейсе нет |
| F14 | Постоянный пул: desired N, резерв, refill, cooldown, квоты | `implemented_unverified` | `pools.py`; `Workbench.pool_refill` вызывается из `api.py`; в интерфейсе `POST /api/pools/create` → 200, `GET /api/pools` → 200 со `state`/`deficit_reason`, `POST /api/pools/action refill` → 200 (живой HTTP, §0.2). **Не сделано: периодический watch** — `grep -rn "pools.watch" proxy_workbench/` даёт 0 вызывающих, пул сам до N не восстанавливается. Квоты по exit-IP нечем хранить: в таблице `pools` нет `quota_basis` |
| F15 | Расписания, timezone/DST, quiet hours, бюджеты, уведомления | `implemented_unverified` | `scheduler.py`; в интерфейсе `POST /api/schedules/action` → 200, `GET /api/schedules` → 200 с интервалом, `timezone`, окнами и бюджетами (§0.2). **`mark_wake()` теперь вызывается продуктом**: `desktop.py:1944` `wake_tick` → `engine.mark_wake()`, `desktop.py:1909` `store.mark_awake(job.id)`; прежняя строка «не имеет ни одного продуктового вызова» больше не верна. **Что осталось**: таблица `schedules` (`db.py:775-777`) по-прежнему несёт 11 колонок и не содержит `last_run_at`, `paused`, `pause_reason`, `dst_policy`, `catch_up`, `wake_gap_s` — интервальная сетка и накопленный бюджет не переживают перезапуск процесса. `SqliteScheduleStore.persists_runtime_state` остаётся `False` |
| F16 | Gateway: binding к пулу+профилю, ротация, sticky, лимиты, transport parity | `implemented_unverified` **(статус прежний, из четырёх пробелов закрыты три)** | Механическое: `gateway.py:604-637` `reserve()` — слот берётся **до** первого `await`; `Binding.pool_id`; `denylist`; session TTL. `4349b53` закрыл три из четырёх пунктов «что осталось» прошлой версии, и это видно по коду, а не по отчёту: (1) **`Binding` доходит до слушателя** — `gui.py:1332-1335` и `proxytool.py:4828-4832` вызывают `gateway.Background(self.data, host, port, token=…, bind=self.gateway_binding(), lan=lan, interface=interface)`; (2) **`lan=True` доходит до сокета** — то же место, `lan=lan`; (3) **`--gateway-interface` существует** — `gui.py:4756` (флаг), `:4811-4815` (отбрасывается без `--lan`), `:4832` (передаётся в `Background`). **Что осталось:** (4) у CLI-команды `gateway` по-прежнему нет `--pool` (`grep -n "--pool" proxy_workbench/proxytool.py` → 0 совпадений); **и я не запускал шлюз ни с `--lan`, ни с привязкой к пулу** — ни одного живого соединения через работающий gateway.Background в эту сессию, доказательства прежние и локальные |
| F17 | Законченный путь подключения: пул → приложение → поля → контроль маршрута → отключение | `implemented_unverified` | Страница шлюза, recipes, QR `app.js:4068` `makeQR`. **Не сделано**: контроль маршрута (last_proxy/pin) в снимке пула отсутствует; **round-trip декодирование QR не проверяется** — `tests/test_qr.py` (29 строк) ищет в исходнике `upward = !upward` и наличие `class="qr-svg"`, но не декодирует QR обратно в исходный URL (R20 отмечал это три цикла подряд) |
| F18 | Единое управление GUI/CLI/API, одинаковые units/codes/permissions | `implemented_unverified` | `api.py` вызывает `profiles.run_request`, `pools`, `jobs`, `scheduler`, `sourcedesk`; новые маршруты интерфейса вызывают те же `workbench.pools()`, `Scheduler`, `importer`, `apikeys`, что и API (§0.2). `tests/test_profiles_parity.py` зелёный. **Не сделано**: `tests/test_parity` не существует; равенство выдачи GUI/API/CLI на одном scope доказано только для профилей |
| F19 | Работа со списком: page/selected/all-matching, bulk, теги, saved views, выделенное не переносится на другой scope | `implemented_unverified` | 26 вхождений `bulk` в `app.js`; `proxytool.py:2758` расчёт `kind` (`selection` / `top` / `published`) и `:2931` отдельная ветка — экспорт выделенного не публикует поколение. Три зелёных теста на это. **Прошла часть, которую раньше считали не начатой**: теги (`gui.py:2493-2514`, `tags_in_use`), избранное и заметки (`gui.py:2191,2345-2346`, сайдкар `gui-annotations.json`), **сохранённые представления** (`gui.py:2599-2629` `saved_views`/`store_view`/`delete_view`, маршруты `GET/POST /api/views`, `/api/views/delete`), **матрица proxy × target** (`gui.py:2260` `matrix`, маршрут `GET /api/results/matrix`). **Не проверено мной**: ни один из этих маршрутов я не вызывал; сравнение запусков (`RES13`) по-прежнему не сделано |
| F20 | Дополнительные измерения: WebSocket, длительность, media, self-hosted reference probe | `implemented_unverified` **(поднят с `in_progress`)** | **Поднят по одной причине: падающего теста больше нет.** Прежний статус держался на двух основаниях, и обе сняты. (1) `tests/test_probes_reference.py:218-224`, требовавший `supported == False` для `websocket_handshake`, `long_lived_connection` и `media_manifest_segment`, заменён на `test_measured_capabilities_name_their_endpoint_budget_and_outcome`, который требует `supported == True` **и** наличия `endpoint`, `budget` и `outcome`. Docstring нового теста прямо объясняет, что прежнее утверждение было верно, когда продукт умел только GET, и стало ложным, когда F20 принёс реальные пробы, — то есть это не «тест подогнан под код». Проверено: `.venv/bin/python -m unittest tests.test_probes_reference` в полном прогоне зелёный, отдельного падения нет. (2) Правка больше не «в полёте» — она в коммитах. Код на месте: `probes.py` `run_websocket`, `run_duration`, `run_media`, `summarize_websocket`, `parse_media_manifest`, лимиты `WEBSOCKET_LIMITS`/`DURATION_LIMITS`/`MEDIA_LIMITS`, состояния `ok \| not_upgraded \| no_pong \| closed \| error`, эндпоинты reference probe `/ws`, `/hold`, `/manifest.m3u8`, `/segment/1` плюс три негативных. `pr.capability_matrix()` отдаёт `websocket_handshake True`, `long_lived_connection True`, `media_manifest_segment True`, `udp_transport False`, `http2_or_http3 False`. **Почему всё-таки не `verified`:** результатов этих проб я не получал — ни одного живого прогона WS/hold/media не было ни в этой, ни в предыдущей сессии. Живой сетевой путь запрещён условиями, а reference probe на loopback я не гонял. Не поддерживаются по-прежнему только `udp_transport` и `http2_or_http3` |
| F21 | Сравнение источников и провайдеров: cohorts, survival, overlap, стоимость пригодного адреса | `implemented_unverified` (было `todo`) | **Функция написана**: `sourcedesk.py:2468` `compare_sources`, `:2808` `compare_suppliers`, `:2846` `compare_cohorts`, `:2719` `survival_across_windows`, `:2447` `_overlaps`, `:2647` `_cohort_warnings`; тесты `tests/test_sourcedesk_compare.py` (43 теста) зелёные. **Не сделано: не подключено ни к одной пользовательской поверхности** — `grep -rn "compare_cohorts\|compare_sources\|compare_suppliers\|survival_across_windows" proxy_workbench/gui.py proxy_workbench/api.py proxy_workbench/apiv1.py proxy_workbench/proxytool.py` даёт 0 совпадений; в CLI нет ни одной такой команды. Функция жива, но снаружи недостижима, поэтому `todo` снят, а `verified` не поставлен |
| F22 | Удобная фоновая работа: меню-бар, close vs quit, повторный запуск, autostart, sleep/wake | `implemented_unverified` **(статус прежний, но две из трёх причин сняты)** | Слой `desktop.py` — `TRAY_HELPER_SOURCE`, `ensure_tray_helper:2339`, `InstanceLock`/`instance_lock_path:1291`, `ControlServer`/`control_request`, `autostart_status:1606`/`enable_autostart:1672`, `PlatformObserver`/`network_fingerprint`/`wake_tick`/`run_startup_migration`. `8be591b` закрыл две вещи, которые прошлая сессия назвала непочиненными. (1) **Helper теперь кладётся в bundle, и spec без него прерывает сборку**: `packaging/proxy-workbench-macos.spec:41-45` вызывает `tray_helper.stage(...)` и `raise SystemExit`, если helper не собран, `:52` кладёт его в `Contents/…`; `packaging/build_macos.py:74-81` `build_tray_helper`, `:201-204` проверяет, что helper на месте, `:220-258` `verify_launch` **запускает собранное приложение и ждёт меню-бар** (`:360-385` `_wait_for_tray`/`_tray_report`), `:478-488` пишет в отчёт `menu_bar_helper` и честно говорит, что запуск с `--no-tray` меню-бар не проверял. Прежнее «в собранном `.app` меню-бара нет» как утверждение о коде **больше не верно**. (2) **Обычный запуск доходит до фонового слоя**: `proxy_workbench/__main__.py:1-40` — пустая командная строка идёт в `proxy_workbench.desktop`, `gui` или `--no-desktop` открывают только интерфейс, всё остальное уходит в CLI. Прежнее «`python -m proxy_workbench` идёт в `gui.main`» больше не верно. (3) **Фоновые события показывает страница**: маршрут `GET /api/desktop` добавлен (`gui.py:4536`), живой HTTP → 200 с `instance`, `journal`, `autostart`, `power`, `network`, `layout`, `frozen`; `app.js:9994` `renderDesktop(await api('/api/desktop'))`, `app.js:10007` `POST /api/desktop/action` для автозапуска. Прежнее «маршрут `GET /api/desktop` в `gui.py` не добавлен» больше не верно. **Почему всё-таки не `verified`:** `ensure_tray_helper` для frozen-сборки по-прежнему честно возвращает «в сборке нет helper меню-бара», если файла нет, и **ни один артефакт на текущей ревизии ни собран, ни запущен** — `build_macos.py` в этой среде не выполнялся, так что строка кода «сборщик кладёт helper и проверяет меню-бар при запуске» остаётся непроверенным утверждением о коде, а не наблюдением. Windows и Linux не проверялись ни разу. Отчёт владельца о 48 проверках из 48 на macOS — **его отчёт, я его не перепроверял** |
| F23 | Поставка Windows/macOS/Linux, per-user пути, обновления, подпись | `external_blocker` **(статус прежний, один подблокер снят, один добавлен)** | **Подблокер «PyInstaller отсутствует, пересборка невозможна без сети» снят.** Проверено: `.venv/bin/python -c "import PyInstaller"` → `6.22.3`. Пересборка macOS в этой среде технически возможна, её просто не делали. **Подблокер «подпись» остался и усилился проверкой:** `codesign -dv "/tmp/pw-dist/Proxy Workbench.app"` → `Signature=adhoc`; манифест `proxy-workbench-2.2.1-macos-arm64.manifest.json` отдаёт `"signed": false`, `"reason": "no signature was found"`, а `"notarization": {"available": false}`. `packaging/release_manifest.py` по-прежнему ставит флаг только из `desktop.signature_of`, который возвращает `True` лишь после того, как инструмент подписи реально проверил файл. **Подблокер «Windows не собирался» остался и подтверждён дважды:** локальная машина — macOS arm64, `iscc` не установлен, и CI это признаёт сам: `gh run list --workflow windows.yml` → `HTTP 404: workflow windows.yml not found on the default branch`; job `windows-artifact` на `windows-2022` существует только в этой ветке и **ни разу не выполнялся**. **Артефакты отстают:** `/tmp/pw-dist/` — версия **2.2.1 от 25.09 21:49** при текущей `2.3.0`. **Интеллект/universal2 не собираются:** `packaging/build_macos.py:38` `SUPPORTED_ARCHES = ('arm64',)`, `:86-90` и `:422` отказывают с честным объяснением, что universal2 требует двух собранных и слитых срезов, чего скрипт не делает. **Всё это `external_blocker`, а не «готово»** |
| F24 | Данные, backup, restore, retention/cleanup с preview | `verified` | CLI: `proxytool.py:4375` `_cmd_backup`, `proxytool.py:4501` `_cmd_backup_rebind` — `backup create\|list\|verify\|preview\|restore\|rollback\|retention\|cleanup\|rebind`, каждое изменяющее действие с `--apply`. **Миграции 1..12 починены** (`db.py:1148` регистрирует `endpoint_id` на каждом соединении, а не только в `_m0`), retention preview и apply считают один план (`_retention_targets`), `rebind_secrets` поднимает `access_revision`/`rotated_at` и печатает `revisions` (`db.py:432-441`). **Живое подтверждение в этой сессии**: `python -m proxy_workbench backup` перечислил `proxies.sqlite3.v0.pre-migration.20260925T220106.bak` и `proxies.sqlite3.v14.pre-migration.20260926T080721.bak` с манифестами — то есть снятие копии перед неаддитивной миграцией работает на промежуточных версиях, а не только на legacy `v0`. **В интерфейсе**: `GET /api/maintenance/cleanup-preview` → 200, `GET /api/maintenance/retention-preview` → 200 с `runnable`/`targets`/`blocked`, `POST /api/maintenance/restore-preview\|restore` (живой HTTP, §0.2) |
| F25 | Помощь и диагностика: коды, bundle с preview/redaction, версии/scope/job health, fixture recipe | `implemented_unverified` **(статус прежний, обе прежние причины сняты)** | **Обе прежние причины закрыты, и это видно по коду и живому HTTP.** Прежнее «ни кнопки диагностического пакета, ни счётчиков воронки, ни объяснения „почему 0 результатов“» **больше не верно**: `gui.py:4095-4128` `diagnostics_view` (воронка, потери с действиями, коды с заголовком и действием, `health_report`, `local_environment`), `gui.py:4144-4165` `diagnostic_needles` (живые секреты процесса для канарейки), `gui.py:4167-4217` `diagnostics_bundle` (preview, редактирование, сохранение в папку данных, `canary_clean`), маршруты `GET /api/diagnostics` и `POST /api/diagnostics/bundle`, карточка `index.html:2423-2489` с кнопками `diag-bundle-preview`/`diag-bundle-save`, `app.js:9757-9830`. Живой HTTP: `POST /api/diagnostics/bundle {"name":"diagnostic.json"}` → **200** с `text` на 1183 символа, `redactions`, `canary_checked`, `canary_clean`, `sample`, `total`, `truncated`, `describe`, `saved`, `preview`. Прежнее «`diagnose health` печатает ложную тревогу» **больше не верно** (см. §0.3). **Почему всё-таки не `verified`:** пакет я **не сохранял** — только preview; `redactions` вернулись пустым списком на пустой установке, то есть замену секретов в живом пакете я не наблюдал; счётчики воронки на реальном прогоне пусты (`rows_total: 0`, `stages: []`, `zero.code: JOB_FAILED`), потому что данных в тестовой папке нет. Один шаг из четырёх — запись файла — не выполнен |
| F26 | OSS, шаблоны, доступность, RU/EN, документация под реальные возможности | `implemented_unverified` **(статус прежний, главная прежняя причина снята)** | **Паритет переводов закрыт полностью.** Прежнее «встроенные `messages.en` и `messages.ru` содержат 1143 и 1217 ключей, а каждый из десяти пакетов ровно 791, то есть свыше 350 новых ключей отсутствуют и `t()` отдаёт английский» **больше не верно**. Своя проверка разбором словарей на HEAD `d9056e5`: `messages.en` — **1275 уникальных ключей**, `messages.ru` — **1275**, симметрическая разность между ними **0**; каждый из десяти пакетов `ui/i18n/*.js` (`de es fr it ja pl pt tr uk zh`) — **1275 уникальных**, разница с `en` **0** по каждому. То есть все двенадцать языков на полном паритете. `CHANGELOG.md` `[Unreleased]` заполнен и описывает волну ремонтов; версия 2.3.0; README.md/README.ru.md теперь знают про `--max-requests`, `--run-max-bytes`, `--count-what` и `bench` (`README.md:253-258`, `README.ru.md:154-159`) и **верно объясняют, что `--workers` — потолок** (`README.md:257`: «**A ceiling, not a number of simultaneous checks.**»; `README.ru.md:154`: «это **потолок**, а не число одновременных проверок»). **Что осталось:** `grep -c "api-key\|/v1/" SECURITY.md CONTRIBUTING.md` = **0** в обоих — документация под `/v1` и менеджер ключей по-прежнему не переписана; `tests/test_qr.py` (строки 14, 21) проверяет исходный текст (`assertIn('upward = !upward', function)` и `class="qr-svg"`), а не round-trip декодирование QR, — это запрещено MASTER-PROMPT §7 и R20 отмечал это три цикла подряд; в `README.md:421` осталась фраза «fail-fast, 128 workers» и «Raise `--workers`», которая рядом с `:257` выглядит противоречиво; **доступность не доведена** — см. отдельный пункт в §10 про мелкий текст и `role="progressbar"` |
| F27 | URL-подписки и жизненный цикл feed | `implemented_unverified` **(статус прежний, главная причина снята)** | `sourcedesk.py` `import_subscription`/`import_clash`/`import_singbox`/`feed_diagnostics`, ETag/Last-Modified/304, last-good, redaction секретных URL. **Прежнее «`REQUESTED_DDL` не исполняется, `SourceDesk` работает против схемы, которой в живой базе нет» больше не верно** — см. F13: `db.py:1189, 1219` создают `source_feed` и `membership_source`, живая база на `user_version = 18` содержит обе. **Что осталось:** ни одного живого `fetch` я не выполнял — ни ETag, ни 304, ни last-good, ни карантин вживую не наблюдали; доказательства на подставных `FeedResult` и на `test_area_ddl_sourcedesk.py`. Это ровно то, чего не хватает до `verified`, и я это не снимаю |
| F28 | Экспорты и согласованные snapshots | `implemented_unverified` **(статус прежний, главная причина снята)** | `exportsvc.py`; общий snapshot contract; `kind='selection'` не публикует поколение; `api.py:270-315` фиксирует поколение один раз и сверяет `_manifest_digest`. **Целевой клиент теперь достижим — прежнее «`client_target`/`client_binary` не заполняются ниоткуда, `singbox_target(None)` всегда даёт `unconfigured/legacy`, клиентская валидация фактически не выполняется» больше не верно.** Три независимых пути: (1) CLI-флаги `--client-target` / `--client-binary` (`proxytool.py:4720, 4723`, с переменными окружения `PROXY_WORKBENCH_SINGBOX_TARGET` / `_BIN`), которые доходят до `ExportOptions` (`proxytool.py:3905`) и до рендера (`proxytool.py:4119`, `6436`); (2) `exportsvc.resolve_client_target(options, directory=root)` (`exportsvc.py:1508`) — цель разрешается из опций, окружения и `client.json` рядом со снимками, так что вызывающий, который вообще не упоминает клиента, всё равно получает записанное решение; (3) `db`-путь не при делах. Своя проверка вызовом: `None` → `unconfigured`, `1.11.0` → `configured`, `1.10.0` → `configured`, `latest` → `configured` (1.15.0), `1.99.0` → `unverified` + `E_EXPORT_TARGET_UNVERIFIED`, `nonsense` → `unknown` + `E_EXPORT_TARGET_UNKNOWN`. **Что осталось:** (1) `sing-box` в этом окружении **не установлен** (`which sing-box` → пусто), поэтому `client_check()` против настоящего бинарника не выполнялся ни разу — доказательства прежние, на фейковых исполняемых файлах (exit 0 / exit 1); (2) GUI и API `client_target` по-прежнему не задают (`grep -n "client_target" proxy_workbench/gui.py` → 0 совпадений; в `api.py:1050` переменная с таким именем — это формат выгрузки, а не целевой клиент), то есть из интерфейса цель по-прежнему не выбрать; (3) прежняя независимая находка про `remove_generation` **снята**: `exportsvc.py:1542` создаёт каталог поколения через `generation.mkdir()` (без `exist_ok`), поэтому `except BaseException: remove_generation(generation)` (`exportsvc.py:1563-1564`) может удалить только каталог, который создал этот же вызов, и чужое поколение стереть уже не может |
| F29 | Полноценное API и менеджер ключей | `implemented_unverified` **(статус прежний, одна из четырёх причин снята)** | **Воспроизведённый дефект с повторной выдачей секрета починен.** Прежнее «повтор `POST /v1/keys` с тем же `Idempotency-Key` отдаёт тот же полный секрет, механизм `apikeys.carries_one_shot`/`without_one_shot` написан и не вызывается нигде, правка из `HANDOFF/fix-keys.md` в дерево не попала» **больше не верно**: `apiv1.py:2061` кладёт в кэш идемпотентности `apikeys.without_one_shot_response(response)`. Проверено вживую — §0.3, первый ответ отдаёт `secret`, повтор с тем же ключом отдаёт тот же `id`, **секрета в теле нет**, `secret_already_shown: true`, в `api_keys` одна строка. Менеджер ключей в интерфейсе на месте и проверен живым HTTP ещё в прошлую сессию: `GET /api/api-keys`, `POST /api/api-keys/bootstrap`, `POST /api/api-keys`, `POST /api/api-keys/action` (`rotate`/`update`/`disable`/`enable`/`revoke`/`delete`), страница `#page-keys`. **Осталось три вещи:** (1) **per-key `concurrency` по-прежнему не работает** — `Principal.concurrency` записывается (`apiv1.py:547`) и больше нигде не читается, `QuotaGuard` (`apikeys.py:1497`) не имеет ни одного продуктового вызова, единственный `ConcurrencyLimiter` пер-серверный; (2) **резервирование** — см. API05; (3) нет read-only subscription secret для клиентов без заголовков |

---

## 2. Дефекты 1–26 (MASTER-PROMPT §3)

| ID | Что означает | Статус | Чем подтверждено |
| --- | --- | --- | --- |
| 1 | Срок годности проходит scanner → БД → GUI/API/export/gateway | `verified` | `core.apply_measurement` пишет `valid_until`; `test_a_re_export_cannot_move_a_measurement_time`, `test_an_unknown_measurement_time_is_not_fresh` (зелёные в прогоне `Ran 3249 tests`). Строка без записанного срока читается как `freshness=unknown` |
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
| 14 | DNSBL: IPv6 reverse, зонные коды, quota≠listed | `implemented_unverified` **(поднят с `in_progress`)** | **Поднят по одной причине: правка больше не «в полёте», а в коммитах.** Прежняя строка «до 12:01 `reputation.py` был нетронут и содержал два дефекта, правка не закоммичена» **больше не верна**. Своя проверка на HEAD `d9056e5`: `grep -n "address.exploded\|startswith(\"127.\")" proxy_workbench/reputation.py` → **0 совпадений**; `reputation.py:182` `reverse_ip` определена и `:191` делегирует `probes.reverse_ip`, зонные коды приходят из `probes.DNSBL_*`, а `_dnsbl_error_code` отличает квоту или отказ в доступе от NXDOMAIN. `probes.py` закрыл код: dotted nibbles, `DNSBL_QUOTA`/`DNSBL_ACCESS`/`DNSBL_LISTED`/`DNSBL_CLEAR`. **Почему не `verified`:** живой DNSBL-запрос я не выполнял — внешняя сеть запрещена условиями, а локального DNSBL-сервера, который можно поднять, у меня нет. Это доказательства на коде и фикстурах, а не наблюдение. Статус `in_progress` был поставлен из-за незакоммиченности, и эта причина ушла |
| 15 | Скорость: верное окно, минимальный sample, insufficient, отдельные transfer/TTFB | `verified` | `probes.py:113` `INSUFFICIENT_SAMPLE`, отдельные замеры transfer и TTFB; `tests/test_probes_*` |
| 16 | Gateway резервирует слот до await; cancel/error освобождает | `verified` | `gateway.py:604-637` `reserve()`/`_pick(reserve=True)`; `tests/test_gateway_reservation.py`, `test_per_proxy_limit` |
| 17 | HTTP upstream health ≠ TCP-open; нет повторов неидемпотентных | `implemented_unverified` | `gateway.py` различает «туннель установлен» / «получен ответ» / «запрос успешен»; `tests/test_gateway_health.py` |
| 18 | Gateway/GUI/API имеют разные secrets; LAN — явный opt-in | `implemented_unverified` **(статус прежний, вторая половина требования закрыта)** | **Секреты разведены и это не изменилось:** `proxytool.py` отдельный `--gateway-token`/`PROXY_WORKBENCH_GATEWAY_TOKEN`, отказ при совпадении с API-токеном, `compose.yml` использует обе переменные, GUI генерирует отдельный токен на запуск. **`--lan` теперь действительно доходит до сокета — прежнее «`lan=True` не передаётся в `gateway.Background`, а `Bind.__post_init__` без `lan=True` отвергает не-loopback адрес, слушатель просто не стартует» больше не верно.** `gui.py:1332-1335` и `proxytool.py:4828-4832`: `gateway.Background(…, bind=self.gateway_binding(), lan=lan, interface=interface)`; `Bind.__post_init__` получает то, что просил пользователь, и явный opt-in состояние видим (`gui.py:1267`). **Чего я не делал:** шлюз с `--lan` я **не запускал** и ни одного соединения через него не установил. LAN-режим доказан чтением кода и 12 тестами в `tests/test_area_gateway_f17.py`, а не наблюдением |
| 19 | Denylist применяется до prefilter и отзывает новые admissions | `implemented_unverified` | `Denylist.match()` до любой сетевой стадии, отзыв admissions; `tests/test_gateway_denylist.py`. Судьба уже открытых streams — отдельная политика |
| 20 | Empty pool никогда не включает DIRECT; конфиги валидируются клиентом | `implemented_unverified` | **fail-closed закрыт**: `formats.py:39,62,72`; `test_an_empty_export_never_selects_a_direct_route`. **Вторая половина открыта**: `client_target`/`client_binary` не заполняются ниоткуда (§F28), `client_check()` покрыт фейковыми исполняемыми файлами (exit 0 / exit 1) |
| 21 | Browser download: picker в активации, одно закрытие, cancel без ошибки, fallback | `implemented_unverified` | `app.js:6082` `downloadFile` — активация до первого `await`, `preventClose: true`, ровно один `close()`/`abort()`, `AbortError` без тоста, fallback через anchor. **Ограничение**: настоящим кликом в Chromium не воспроизведено — драйвера браузера в окружении нет |
| 22 | Источники: путь update, понятные имена, metadata после collect, карантин | `in_progress` **(статус прежний)** | Без изменений по коду на HEAD `d9056e5`, кроме одного: схема исключений области теперь есть в БД (см. F13), так что `source_management.py:239` больше не упирается в отсутствующую таблицу. Прежние отмеченные остатки в силе: `branding.py:18` `SOURCES_URL` по-прежнему указывает на `main/sources.json`; `country_resolver` (`proxytool.py:3718`, вызывается на `:6390`) по-прежнему не читает `candidate_meta`, собранную этим же прогоном; **ни одного живого `update`/`fetch` я не выполнял** — ни 304, ни last-good, ни карантин вживую; `sourcedesk.py:1345` `retry_after_from_response` есть, но сервер, который его вызовет, я не поднимал. Четыре маршрута источников, `public_source` с `core.source_key`, `source_quality` без фильтров экспорта, `prune_sources` — всё на месте (данные §0.2) |
| 23 | Quick test подписан объёмом; full recheck отдельно | `implemented_unverified` **(статус прежний, прежняя причина снята)** | **Прежнее «не сделано в GUI: `App.start({'action':'quick'})` без полного подписанного бюджета» больше не верно.** `gui.py:2556-2620` `test_proxy`: docstring прямо называет дефект и способ закрытия, а тело перед тем, как потратить, сообщает объём — `planned_requests = attempts * max(1, len(target_names))`, `deadline_s = QUICK_TEST_DEADLINE_S`, `scope = dict(kind='quick_diagnostic', stored=False, targets=…, attempts=…, planned_requests=…, max_request_bytes=…, request_timeout_s=…, whole_deadline_s=…, skipped=['reputation_pipeline','anonymity_judge','bandwidth'], recheck_action='recheck', recheck_note=…)`. То есть объём назван, потолок стоит, что именно **не** измерялось — перечислено, и полная перепроверка предлагается отдельным действием, а не подразумевается. **Почему не `verified`:** кнопку быстрого теста я не нажимал; доказательство здесь чтение кода. `/v1` quick-test по-прежнему требует `budget`/`timeout` в теле и отвечает 202 (`tests/test_api.py`) |
| 24 | Presets не оставляют параметры прошлого сценария; website ≠ весь сервис | `implemented_unverified` | `servicecatalog.py` versioned presets с явным составом и сбросом полей; `capability_matrix()` различает homepage/API/WebSocket/media; `not_proved` и `limitations_ru` раскрыты. **Не сделано**: место показа diff обновления пользователю |
| 25 | Live feed использует реальные события измерений | `in_progress` | Сторона `jobs.py` закрыта: реальные события пишутся в `job_event` по одному на item. **Не сделано**: живая лента в UI по-прежнему строится из хвоста `data/gui-events.jsonl`; `JobStore._emit` из `store()` в `proxytool.py` не вызывается |
| 26 | Реальная Mac-поставка, writable paths, Windows lifecycle, достоверные release notes | `external_blocker` **(статус прежний, один подблокер снят)** | **Меню-бар теперь доходит до bundle** — `packaging/proxy-workbench-macos.spec:41-52` кладёт Swift-helper и прерывает сборку без него, `build_macos.py:220-258` проверяет меню-бар при запуске собранного приложения. Прежнее «даже пересобранный `.app` не получит меню-бара, пока `build_macos.py` и spec не положат helper в bundle» **больше не верно как утверждение о коде**. **Подблокер PyInstaller снят:** `import PyInstaller` → `6.22.3`, пересборка возможна. **Подблокеры, которые остались:** артефакты отстают (`/tmp/pw-dist` — 2.2.1 от 25.09 при 2.3.0) и **ни один артефакт на текущей ревизии не пересобран и не запущен**; подпись и нотаризация macOS — `codesign` даёт `Signature=adhoc`, манифест `"signed": false`, `"notarization": {"available": false}`, ключей нет; Windows — `gh run list --workflow windows.yml` → `404 … not found on the default branch`, job существует только в этой ветке и не выполнялся. Всё это `external_blocker` |

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
| R09 IPv6 DNSBL и коды не исправлены | `implemented_unverified` **(поднят с `in_progress`)** | Дефект 14: `reputation.py:182-191` делегирует `probes.reverse_ip`, прежние разворот IPv6 и правило «любой `127.*` = listed» в дереве отсутствуют (проверено на HEAD `d9056e5`), правка в коммитах. Живых DNSBL-запросов по-прежнему не было — это единственное, что не даёт `verified` |
| R10 Измерение Mbps прежнее | `verified` | Дефект 15; `INSUFFICIENT_SAMPLE` |
| R11 Gateway reservation и handshake deadline | `verified` | Дефект 16; `tests/test_gateway_reservation.py` |
| R12 LAN по умолчанию и тот же секрет | `implemented_unverified` **(статус прежний, вторая половина закрыта)** | Дефект 18: секреты разведены; `lan=lan` теперь доходит до `gateway.Background` (`gui.py:1335`, `proxytool.py:4831`). Шлюз с `--lan` я не запускал |
| R13 Denylist не отзывает уже выданный пул | `implemented_unverified` | Дефект 19; отзыв admissions сделан, судьба открытых streams — отдельная policy |
| R14 sing-box нуждается в versioned validation | `in_progress` | Дефект 20: fail-closed есть, `client_target` недостижим — единственные непустые значения задаёт только тест |
| R15 Скачивание закрывает stream дважды | `implemented_unverified` | Дефект 21; проверено под стабами, не в браузере |
| R16 Источники в main улучшены лишь частично | `in_progress` **(статус прежний)** | Совпадает с дефектом 22: четыре маршрута и `source_quality` починены, схема источников теперь создаётся в БД (схема 18), но каталог по-прежнему указывает на `main/sources.json` (`branding.py:18`), `country_resolver` по-прежнему не читает `candidate_meta` текущего прогона, и ни одного живого `fetch` не было |
| R17 Smart presets и «живая лента» сильнее backend | `in_progress` | Совпадает с дефектами 24 и 25: presets починены с честным `not_proved`, живая лента — нет |
| R18 Быстрый тест — диагностический | `implemented_unverified` **(статус прежний, прежняя причина снята)** | Дефект 23: `gui.py:2556-2620` называет объём до того, как тратит его, и перечисляет, что не измерял. Кнопку я не нажимал |
| R19 Desktop-цель не закрыта | `external_blocker` **(статус прежний, состав блокеров изменился)** | F23/дефект 26: **helper меню-бара теперь попадает в bundle** и spec без него прерывает сборку, так что прежняя причина снята; **PyInstaller 6.22.3 установлен**, пересборка возможна. Остались: артефакты 2.2.1 от 25.09 и ни одна пересборка на текущей ревизии; подписи нет; Windows-job (`windows-2022`) ни разу не выполнялся — `gh run list --workflow windows.yml` → 404; `SUPPORTED_ARCHES = ('arm64',)`, Intel/universal2 не собираются |
| R20 Инженерная приёмка и документация отстают | `implemented_unverified` **(статус прежний, состав остатков изменился)** | **Появился первый тест, который смотрит на интерфейс, а не на HTTP:** `tests/test_web_bundle_init.py` — 4 теста, проверены в обе стороны (падают при возврате бага, §0.4). Это прямой ответ на случай `closeDetailsBtn`, который 3245 тестов и 110 живых API-проверок не поймали. **Закрыто:** README теперь знает про `--max-requests`, `--run-max-bytes`, `--count-what` и `bench` (`README.md:253-258`) и верно объясняет, что `--workers` — потолок (`README.md:257`, `README.ru.md:154`). **Осталось:** `tests/test_qr.py:14, 21` проверяет исходный текст, а не декодирование QR; browser E2E отсутствует; `SECURITY.md` и `CONTRIBUTING.md` по-прежнему содержат 0 совпадений по `api-key\|/v1/`; в `README.md:421` осталась фраза «fail-fast, 128 workers» рядом с `:257` |

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
| A-F13 IPv6 DNSBL reverse с двоеточиями | дефект 14 | `implemented_unverified` — `probes.py` починен, `reputation.py:191` переведён на `probes.reverse_ip` и правка в коммитах (проверено на `d9056e5`); живым DNSBL-запросом не проверена |
| A-F14 Любой `127.*` = listed | дефект 14 | `implemented_unverified` — правила в дереве нет (`grep` по `startswith("127.")` в `reputation.py` даёт 0 на `d9056e5`), правка в коммитах; живым запросом не проверена |
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
| A-F32 `normalize()` отбрасывает hostname/auth/private | F04 | `in_progress` **(статус прежний, auth больше не отбрасывается везде)** — hostname — да; измеряющая сторона — по-прежнему да (`probes.py:710`, `proxytool.py:138, 333` отвергают `username`/`password` в URL), но шлюз такие адреса теперь довооружает: `gateway.py:293` `AccessCredentials`, `Proxy-Authorization: Basic` по RFC 7617, `secrets.transport_credentials` |
| A-F33 `paths.default_data()` для frozen пишет рядом с exe | F23 | `implemented_unverified` (`desktop` задаёт `PROXY_WORKBENCH_DATA`, но сырой frozen-CLI — нет) |
| A-F34 Тема/язык в localStorage случайного порта | UX14 | `todo` **(перепроверено, статус прежний)** — `app.js:2660-2677` по-прежнему привязывает язык к коду, а не к порту; кнопка темы `app.js:6972-6980` переключает `data-theme`, но ключ хранения по-прежнему один на все запуски |
| A-F35 RAM не ограничена; API грузит полный JSON | ENG06/ENG07 | `in_progress` — `Budgets` теперь ограничивает RAM (`max_ram_bytes`, `ram_per_inflight_bytes`, `proxytool.py:1974-1982`), но legacy `/proxies` сохранил `MAX_LIMIT=1_000_000` |
| A-F36 Нет временной истории, миграций, именованных профилей | ENG03/ENG04 | `verified` (`db.py` миграции 0..15, `observations`, `profile_revision`; миграции 1..12 починены — `db.py:1148`) |
| A-F37 Нет ETag/cache, общего бюджета и deadline пагинации | SRC07/SRC09 | `implemented_unverified` (ETag/Last-Modified/304 есть; живых 304 я не наблюдал) |
| A-F38 Нет экрана отклонённых, 13 колонок, нет `role=progressbar` | F10 / UX11 | `in_progress` **(перепроверено, статус прежний, экран отклонённых теперь есть)** — CLI-диагностика была и раньше; GUI-экран с воронкой и потерями появился в `d917d91` (`index.html:2423-2489`, `app.js:9757-9830`, живой HTTP 200), но `role="progressbar"` в `index.html` по-прежнему 0 совпадений, а полосы воронки размечены `role="img"` с `aria-label` (`app.js:9788`) — это не прогресс-роль |
| A-F39 README противоречит флагам; AGENTS/CONTRIBUTING расходятся | F26 | `implemented_unverified` — `--workers` в README описан как число воркеров, новых флагов и `bench` там нет |
| A-F40 Нет browser E2E и packaged-Mac проверки; фиксированные порты | R20 / F23 | `implemented_unverified` **(перепроверено, статус прежний)** — динамические порты в smoke на месте; browser E2E по-прежнему нет, но появился `tests/test_web_bundle_init.py`, который ловит класс «скрипт падает при загрузке» без браузера; packaged-Mac на текущей ревизии нет — `build_macos.py` не выполнялся, артефакты 2.2.1 |

---

## 5. K-F01–K-F28 — 28 принятых карточек продуктового исследования

| ID | F | Статус | Приёмочный сценарий карточки |
| --- | --- | --- | --- |
| K-F01 | F01 | `implemented_unverified` | Частично: режимы в CLI есть, «найти 5 рабочих» не гонял |
| K-F02 | F02 | `verified` | Закрыт (`test_a_personal_import_after_a_big_public_sweep_checks_only_that_collection` + живой импорт в коллекцию через HTTP, §0.2) |
| K-F03 | F03 | `verified` | Закрыт на CLI (`test_a_repeated_import_commit_does_not_duplicate_membership`) и в интерфейсе: живой HTTP §0.2 — предпросмотр, commit с номерами отклонённых строк, идемпотентный повтор. Форматы `json`, режим `replace` и drag-and-drop не гонял |
| K-F04 | F04 | `implemented_unverified` **(поднят с `in_progress`)** | Транспорт аутентификации появился в шлюзе: `Proxy-Authorization: Basic` (RFC 7617) и `username/password` для SOCKS5/5h, 13 тестов `tests/test_area_gateway_credentials.py`. Не закрыто: измеряющая сторона по-прежнему отвергает пароль в URL; живого соединения к прокси с учётными данными не было; OS-vault не проверен — `keyring` в `.venv` нет |
| K-F05 | F05 | `implemented_unverified` | Частично: parity профилей доказан, исполнение профиля GUI/CLI — нет |
| K-F06 | F06 | `implemented_unverified` | Частично: каталог виден в UI, diff обновления пользователю не показывается |
| K-F07 | F07 | `implemented_unverified` | Частично: единицы и лимиты есть; `allow_private` в `/v1/collections` по-прежнему игнорируется |
| K-F08 | F08 | `in_progress` | Не закрыт: один критерий для GUI/API/export не внедрён, схема не держит exit-модель |
| K-F09 | F09 | `implemented_unverified` | Закрыт на уровне admission; сквозной путь до GUI/gateway/new connection не гонял |
| K-F10 | F10 | `implemented_unverified` **(статус прежний, обе прежние причины сняты)** | В GUI теперь есть: `GET /api/diagnostics` → 200 с воронкой, потерями, кодами и `health`; карточка `index.html:2423`. `diagnose health` больше не печатает ложную тревогу (`5 checks, 1 problems`). Не закрыто: счётчики на реальном прогоне не наблюдались, кнопка восстановительного действия не нажата |
| K-F11 | F11 | `verified` | Закрыт (`tests/test_jobs_*` + живой прогон с job ID) |
| K-F12 | F12 | `implemented_unverified` | Закрыт по стадиям, бюджетам, find-N и трём единицам (живой прогон §0.1). Обоснование выбора `EXPENSIVE_ALL_PASSING` теперь записано в коде, `proxytool.py:3382-3402`. Не закрыт: поток `collect → проба` отсутствует, `--run-max-bytes` числом не подтверждён |
| K-F13 | F13 | `implemented_unverified` | Закрыт по четырём маршрутам источников (живой HTTP §0.2) **и по схеме**: `source_feed`/`membership_source` теперь создаются (`db.py:1189, 1219`), живая база на `user_version = 18` содержит обе. Не закрыт: живых fetch/304/last-good не было |
| K-F14 | F14 | `implemented_unverified` | Закрыт по refill в CLI и в интерфейсе; не закрыт по автономному watch |
| K-F15 | F15 | `implemented_unverified` | Закрыт по DST/слиянию слотов после сна/бюджетам/уведомлениям, и `mark_wake`/`mark_awake` теперь вызываются из `desktop.py`; не закрыт по переживанию перезапуска — в таблице `schedules` нет `last_run_at`/`paused` |
| K-F16 | F16 | `implemented_unverified` | Частично: reservation/auth/лимиты, привязка настраивается в интерфейсе; LAN из GUI недоступен, `Binding` до слушателя не доходит, `--pool` в CLI нет |
| K-F17 | F17 | `implemented_unverified` | Не закрыт: контроль маршрута отсутствует, round-trip QR не проверяется |
| K-F18 | F18 | `implemented_unverified` | Закрыт на CLI+API+GUI (живой HTTP §0.2 показывает общие вызовы); `tests/test_parity` отсутствует |
| K-F19 | F19 | `implemented_unverified` | Закрыт в части «выделенное не переносится на другой scope» (три теста). Теги, заметки, saved views и матрица теперь написаны, но мной не вызывались |
| K-F20 | F20 | `implemented_unverified` **(поднят с `in_progress`)** | Падавший тест заменён на `test_measured_capabilities_name_their_endpoint_budget_and_outcome` (`tests/test_probes_reference.py:218-232`), который требует `supported == True` и наличия `endpoint`/`budget`/`outcome`; правка в коммитах. Прогонов этих проб по-прежнему не было — живую сеть не трогали |
| K-F21 | F21 | `implemented_unverified` | Функция написана и покрыта 43 тестами, но **ни к одной поверхности не подключена** — карточка не закрыта |
| K-F22 | F22 | `implemented_unverified` | Три прежние причины сняты: helper кладётся в bundle и spec без него прерывает сборку; обычный запуск доходит до `desktop.main` (`__main__.py:1-40`); фоновые события показывает страница (`GET /api/desktop` → 200, `app.js:9994, 10007`). Не закрыто: ни один артефакт на текущей ревизии не собран и не запущен, Windows/Linux не проверялись |
| K-F23 | F23 | `external_blocker` | Не закрыт на текущей ревизии |
| K-F24 | F24 | `verified` | Закрыт в CLI (включая миграции 1..12 и копию перед неаддитивным шагом — живое подтверждение в §F24) и в интерфейсе (предпросмотры, §0.2) |
| K-F25 | F25 | `implemented_unverified` | В GUI теперь и воронка, и пакет: `POST /api/diagnostics/bundle` → 200 с текстом на 1183 символа и канарейкой; `diagnose health` больше не сломан. Не закрыто: сохранение пакета не выполнялось, `redactions` на пустой установке пусты |
| K-F26 | F26 | `implemented_unverified` | **Паритет 12 языков полный**: en и ru по 1275 ключей, симметрическая разница 0, каждый из десяти пакетов 1275 и совпадает с en. README флаги знает и `--workers` объясняет верно. Осталось: SECURITY/CONTRIBUTING не переписаны под `/v1`; `tests/test_qr.py` проверяет текст; в `README.md:421` осталось «128 workers» |
| K-F27 | F27 | `implemented_unverified` | `REQUESTED_DDL` теперь исполняется — DDL в `db.py:1189, 1219`, живая база на схеме 18. Не закрыто: живых fetch/304 не было |
| K-F28 | F28 | `implemented_unverified` | **Целевой клиент стал достижим**: `--client-target`/`--client-binary` (CLI) и `resolve_client_target` из опций, окружения и `client.json`; `singbox_target` отдаёт `configured`/`unsupported`/`unverified`/`unknown`. Частично: snapshot contract и selection-артефакт. Не закрыто: бинаря `sing-box` в окружении нет, из GUI/API цель не выбрать |

---

## 6. 140 задач BACKLOG.ru.md

### 6.1 Каталог и сбор источников (SRC01–SRC20)

| ID | Статус | Примечание |
| --- | --- | --- |
| SRC01 каталог с ID/publisher/family/parser | `implemented_unverified` | `sourcedesk.py` + `sources.json` |
| SRC02 карточки источников, фильтры | `implemented_unverified` | В CLI/API/интерфейсе есть; карточки каталога по-прежнему не полны |
| SRC03 журнал fetch | `implemented_unverified` | `feed_diagnostics` в `sourcedesk`; GUI-экран отсутствует |
| SRC04 раздельные счётчики raw/valid/unique/new/passed | `implemented_unverified` | Есть в отчётах источников |
| SRC05 many-to-many provenance, first/last-seen | `implemented_unverified` | `candidate_seen` даёт many-to-many; `membership_source` теперь создаётся в живой базе (`db.py:1219`, `user_version = 18`) |
| SRC06 группировка зеркал и dataset-family | `implemented_unverified` | Поле family в каталоге; `compare_sources` группирует одинаковые наборы в семейства (тесты) |
| SRC07 ETag/Last-Modified/304/cache | `implemented_unverified` | Заявлено в `sourcedesk`; живых 304 я не наблюдал |
| SRC08 per-host concurrency, Retry-After, backoff | `implemented_unverified` | Есть в сборщике |
| SRC09 общий бюджет сбора | `implemented_unverified` | `CHANGELOG`: 32 МиБ / 500 000 кандидатов на источник |
| SRC10 карантин вместо prune | `in_progress` | `quarantine_until` теперь создаётся в живой базе; `prune_sources` правит только настройки, экран карантина в GUI по-прежнему нет |
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
| CHK13 DNSBL zone adapters, IPv6, bounded cache | `implemented_unverified` **(поднят с `in_progress`)** | Дефект 14: `probes.py` закрыл код, `reputation.py:191` переведён на `probes.reverse_ip`, правка в коммитах. Живых DNSBL-запросов по-прежнему не было |
| CHK14 раздельные endpoint/exit геолокация, ASN | `implemented_unverified` | F08; не внедрено в API/GUI |
| CHK15 самостоятельно размещаемый echo/judge | `implemented_unverified` | `probes.serve_reference_probe` |
| CHK16 корректный bandwidth с min-duration | `verified` | Дефект 15 |
| CHK17 WebSocket и длительное соединение | `implemented_unverified` **(поднят с `in_progress`)** | Реализованы в `probes.py` вместе с media; падавший тест матрицы заменён на `test_measured_capabilities_name_their_endpoint_budget_and_outcome`, `d9056e5` зелёный. Результатов проб я по-прежнему не получал. Не поддерживаются только `udp_transport` и `http2_or_http3` |
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
| POO05 бюджеты времени/трафика/requests | `implemented_unverified` | **Понижение остаётся в силе** — единственное из трёх, что я не снял: `--max-requests` проверен живым прогоном (`checked: 1, requests: 1, E_LIMIT_BUDGET`), а `--run-max-bytes` на фикстуре, где тело ответа не читается, не срабатывает (`bytes: 0`). «Бюджеты платят и то, и другое» подтверждено наполовину |
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
| API04 health/readiness с причинами empty/stale | `implemented_unverified` | Ложная тревога `diagnose health` **починена** (проверено вживую: `5 checks, 1 problems`, §0.3), `reader_state` в API отвечает корректно, `health_report` отдаётся интерфейсом в `GET /api/diagnostics`. Прежнее понижение снято; остаётся то, из-за чего статус и был не `verified`: фильтры на legacy `/proxies` и на `/v1` разные |
| API05 lease/acquire/release с TTL | `in_progress` | `reservations.lease/acquire/release` вызывают один `_reservation`, который читает текущий снимок и отдаёт первые N строк; `lease_id`, `ttl_s` и `state` из тела **игнорируются**, ничего не бронируется и не освобождается |
| API06 bounded client feedback | `todo` | Не реализован |
| API07 SSE/events о заданиях | `verified` | `test_an_event_stream_answers_with_frames_and_stops_on_its_own` |
| API08 подписки PAC/Clash/sing-box с ETag | `implemented_unverified` | `exportsvc`; клиентская проверка версии не достижима (`client_target` пуст) |
| API09 проверенные примеры curl/Python/JS | `in_progress` | Полный сценарий есть в docstring `apiv1.py`; в README/SECURITY не перенесён |
| API10 именованные пулы/порты для приложений | `implemented_unverified` | `Binding.pool_id`; в интерфейсе привязка настраивается, но `Binding` не передаётся в слушатель; `--pool` в CLI нет |
| API11 единые upstream transports | `implemented_unverified` | capability matrix; HTTPS-to-proxy parity в gateway не завершена |
| API12 token scopes, rotation, TLS reverse proxy | `implemented_unverified` | Скоупы, ротация и отзыв работают (живой HTTP §0.2), `test_the_legacy_token_reads_and_gains_nothing` зелёный. **Повторная выдача секрета по `Idempotency-Key` починена** — `apiv1.py:2061`, проверено вживую (§0.3). Прежнее понижение снято; остаётся пер-ключевой `concurrency` (см. §10.3) и отсутствие read-only subscription secret |

### 6.7 Удобство интерфейса (UX01–UX15)

| ID | Статус | Примечание |
| --- | --- | --- |
| UX01 первый запуск без терминала | `in_progress` | Текст справки объясняет сценарии; мастер выбора цели отсутствует |
| UX02 простой и расширенный режимы | `verified` | Переключатель в UI есть |
| UX03 именованные шаблоны с точным описанием | `implemented_unverified` | `servicecatalog` + `not_proved` |
| UX04 мастер подключения и «Проверить подключение» | `in_progress` | Recipes есть; контроль маршрута (last_proxy/pin) отсутствует |
| UX05 понятные пустые/ошибочные состояния | `implemented_unverified` **(поднят с `in_progress`)** | Экран воронки и объяснение «почему 0 результатов» теперь в GUI (`GET /api/diagnostics` → 200 с `zero`, `zero_text`, `losses`, `codes`; `index.html:2423`). Не проверено: счётчики на реальном прогоне (на пустой базе `stages` пуст), кнопка восстановительного действия |
| UX06 structured i18n с ключами | `implemented_unverified` **(статус прежний, прежняя причина снята)** | `error.langPack`; 12 языков, и **все на полном паритете**: en и ru по 1275 уникальных ключей, симметрическая разница 0, каждый из десяти пакетов 1275 и совпадает с en. Не проверено: живой перебор всех двенадцати языков в интерфейсе глазами |
| UX07 вынос локализаций | `implemented_unverified` | `ui/i18n/*.js`, десять пакетов по 1275 ключей; `tests/test_i18n.py` проверяет определение языка, кодировку и перевод CLI-строк, но **не паритет ключей** — паритет я проверил сам разбором словарей, и он полный |
| UX08 loading/offline/reconnecting и черновики | `todo` | Не реализовано |
| UX09 клавиатурные команды | `todo` | Не реализовано |
| UX10 клавиатурная навигация и фокус | `in_progress` **(поднят с `todo`)** | **Кольцо фокуса в коде есть:** глобальное правило `style.css:9335-9339` `:focus-visible { outline: 2px solid; outline-offset: 2px }`, 75 правил с `:focus`, кольцо заменено на `box-shadow` там, где `outline` снят (`:11947-11952` `.button:focus-visible`, `:13474` `select:focus-visible`, `:17636` `.code-editor-input`). **Почему не выше:** поведение фокуса при настоящем нажатии Tab я **не проверял** — автоматизация браузера в этой сессии недоступна, `role="progressbar"` в `index.html` 0 совпадений |
| UX11 доступный прогресс для screen reader | `todo` | `role="progressbar"` в `index.html` — 0 совпадений; полосы воронки размечены `role="img"` с `aria-label` (`app.js:9788`), то есть это подпись, а не прогресс-роль. `aria-live` есть (4 вхождения в `index.html`) |
| UX11 доступный прогресс для screen reader | `todo` | Не реализовано |
| UX12 адаптация 1366×768, 200% zoom, reduced motion | `in_progress` **(поднят с `todo`)** | `prefers-reduced-motion` — 22 вхождения в `style.css`, то есть уважение к настройке в коде есть. 200% zoom и 1366×768 **не проверялись**: браузер недоступен, а все 551 объявление `font-size` записаны в `px` (ни одного `rem`), так что при увеличении браузера текст масштабируется только средствами браузера, а не макетом |
| UX13 отмена устаревших запросов и debounce | `todo` | Не реализовано |
| UX14 сохранение темы/языка/колонок/профиля | `todo` | A-F34 не исправлен: localStorage привязан к порту случайного запуска |
| UX15 настройки с preview, backup/reset, диагностика | `implemented_unverified` **(статус прежний, диагностика теперь в GUI)** | Предпросмотр уборки, retention и восстановления есть (живой HTTP §0.2). **GUI-диалог диагностики и пакетный bundle появились:** `GET /api/diagnostics` и `POST /api/diagnostics/bundle` → 200, `index.html:2423-2489`, `app.js:9757-9830`. Не закрыто: сохранение пакета не выполнялось, `redactions` на пустой установке пусты |

### 6.8 Desktop и установка (DES01–DES14)

| ID | Статус | Примечание |
| --- | --- | --- |
| DES01 per-user data/cache/logs и portable mode | `implemented_unverified` | `desktop.resolve_layout`; `paths.default_data` для сырого frozen по-прежнему рядом с exe |
| DES02 самодостаточная Mac arm64 `.app` и `.dmg` | `implemented_unverified` **(статус прежний, одна причина снята)** | Артефакт версии 2.2.1 от 25.09 существует; на текущей 2.3.0 не пересобран. **`PyInstaller` 6.22.3 теперь установлен**, то есть прежняя причина «пересборка невозможна без сети» снята — пересборка просто не делалась. `build_macos.py` в этой сессии не выполнялся |
| DES03 Intel/universal2 | `external_blocker` | `build_macos.py:38` `SUPPORTED_ARCHES = ('arm64',)`, `:86-90` и `:422` отказывают с честным объяснением, что universal2 требует двух собранных и слитых срезов. PyInstaller теперь есть, но Intel-срез на этой arm64-машине собрать нечем, так что внешний блокер остаётся |
| DES04 Windows installer per-user и portable ZIP | `external_blocker` **(перепроверено)** | `build_windows.py` и `windows-installer.iss` приведены к рабочему состоянию, `build_windows.py:211` строк правок в волне `8be591b`. **Ни разу не собрано:** локальная машина — macOS arm64, `iscc` не установлен, а CI-job `windows-artifact` (`windows-2022`) не выполнялся ни разу — `gh run list --workflow windows.yml` → `HTTP 404: workflow windows.yml not found on the default branch` |
| DES05 native desktop window | `todo` | Тонкий desktop host поверх текущего UI; нативного окна нет |
| DES06 иконки, metadata, About, версии | `implemented_unverified` | Метаданные есть, иконка/Dock не проверены |
| DES07 tray/menu bar | `implemented_unverified` **(статус прежний, прежняя причина снята)** | `desktop.py` `TRAY_HELPER_SOURCE`/`ensure_tray_helper:2339`/`Tray` — слой написан. **Helper теперь кладётся в bundle:** `packaging/proxy-workbench-macos.spec:41-45` прерывает сборку, если helper не собран, `:52` кладёт его внутрь; `build_macos.py:74-81, 201-204, 220-258` собирает, проверяет наличие и **при запуске готового приложения ждёт меню-бар** (`:360-385`). **Почему не `verified`:** ни один артефакт на текущей ревизии не собран и не запущен, так что строка кода остаётся непроверенным утверждением; отчёт владельца о 48 проверках из 48 я не перепроверял |
| DES08 single instance с безопасным IPC | `implemented_unverified` | `InstanceLock`, `instance_lock_path:1291`, `ControlServer`/`control_request`; `GET /api/desktop` → 200 отдаёт состояние `instance`. Повторный запуск и IPC по-прежнему мной не проверялись |
| DES09 lifecycle workers и завершение | `implemented_unverified` | `desktop` закрывает handlers; `Tray.reap()`/`Tray.stop()` и `interface_holder()` описаны в handoff; проверка на собранном `.app` относится к старой ревизии |
| DES10 автозапуск по явному выбору | `implemented_unverified` (было `todo`) | `autostart_status:1606`, `enable_autostart:1672`, LaunchAgent / `HKCU\…\Run` / XDG. macOS-путь описан и, по отчёту владельца, проверен; **Windows-ветка (`winreg`) и XDG не запускались ни разу** |
| DES11 обновления с проверкой подписи и rollback | `implemented_unverified` | `desktop.plan_update`/`apply_update`/`rollback` — preview по умолчанию; публикующего сервера нет |
| DES12 signing и notarization в release pipeline | `external_blocker` | Ключей нет, не создавались и не покупались; манифест `"signed": false`; `release_manifest.py` берёт флаг только из реальной проверки подписи |
| DES13 установочные проверки чистой системы | `external_blocker` | Нет Windows-машины (и CI-job не выполнялся); macOS-установка на текущей ревизии не проверялась — `packaging/verify_delivery.py` написан и проверяет per-user пути и перенос старой папки на настоящем артефакте, но артефакт 2.2.1 |
| DES14 системный proxy (P3) | `not_applicable_with_evidence` | `BACKLOG.ru.md:221` относит системный proxy/TUN к «за пределами плана релиза»; MASTER-PROMPT §0 запрещает включать системный proxy на машине пользователя в эту реализацию |

### 6.9 Архитектура, данные и качество (ENG01–ENG14)

| ID | Статус | Примечание |
| --- | --- | --- |
| ENG01 разделить collector/checker/storage/jobs/selection/export | `verified` | 40 доменных модулей в `proxy_workbench/*.py`; движок импортирует их, а прогон ведёт `pipeline.Pipeline` |
| ENG02 типизированные модели | `implemented_unverified` | Dataclass-ы есть; полной schema-проверки нет |
| ENG03 observations и latest summaries | `verified` | `observations` (миграция 4), одна строка `results` на адрес (миграция 15) |
| ENG04 версионированные migrations, backup, restore | `verified` | `db.py` миграции 0..**18** (схема поднята 15→18 в `33cec72`: `source_feed`, `membership_source`, `candidate_scope_exclusion`), `VACUUM INTO`, restore/rollback в CLI, **починка миграций 1..12** (`db.py:1148`) и живое подтверждение копий `v0`/`v14` (§F24). Своя проверка: `PRAGMA user_version` = 18, три новые таблицы в `sqlite_master` |
| ENG05 retention и cleanup с preview | `verified` | `retention_preview`/`apply_retention` считают один план (`_retention_targets`), заблокированные строки считаются нулём; `cleanup_preview` + `backup retention\|cleanup --apply`; в интерфейсе предпросмотры отвечают 200 (живой HTTP) |
| ENG06 индексы и materialized summaries | `implemented_unverified` | Индексы миграции 14; `MAX_PAGE_LIMIT` для `/v1` |
| ENG07 ограничить RAM и число соединений | `implemented_unverified` (было `in_progress`) | `Budgets` теперь тратит дескрипторы и RAM по-настоящему (`proxytool.py:1974-1982`, `acquire(fds=…, ram=…)`), потолок `worker_ceiling()` двигается по наблюдаемой доле успеха. Живого измерения потолка RAM/FD я не делал |
| ENG08 общая schema validation | `implemented_unverified` | `E_VALIDATION_*`; `allow_private` — контрпример (не изменился) |
| ENG09 regression/contract suite | `verified` | `Ran 3249 tests in 231.391s`, `OK (skipped=1)`, код выхода 0 — прогон снят мной на HEAD `d9056e5`. **Оговорка, которая теперь важнее самого числа:** до `8e24d23` этот прогон оставался зелёным, пока интерфейс был полностью мёртвым, потому что ничего из него не смотрит на `app.js`. Сейчас смотрит `tests/test_web_bundle_init.py` |
| ENG10 browser E2E + accessibility | `in_progress` **(поднят с `todo`)** | Появился первый тест, исполняющий настоящий `app.js` под DOM-заглушкой в node (`tests/test_web_bundle_init.py`, 4 теста, проверены в обе стороны). **Browser E2E по-прежнему нет** — драйвера в окружении нет, автоматизация браузера в этой сессии недоступна. Доступность: кольцо фокуса и `prefers-reduced-motion` в коде есть, `role="progressbar"` нет, мелкий текст 8.5–10.5px на 550 правилах есть (см. §10) |
| ENG11 performance benchmarks с baseline | `implemented_unverified` | `pipeline.benchmark` (синтетика, честно помечена) и команда `bench` в CLI; живых замеров нет. **Побочно найдено и по-прежнему не исправлено:** `db.upsert_endpoint` выполняет `PRAGMA table_info` на каждый вызов — `db.py:507-508` `columns()` без кэша, `db.py:2221` вызывает её всегда; `grep -n "lru_cache\|_columns_cache\|COLUMN_CACHE" proxy_workbench/db.py` → 0 совпадений на HEAD `d9056e5` |
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
| OSS05 translation guide и синхронизация EN/RU | `implemented_unverified` **(поднят с `in_progress`)** | **Паритет полный:** en и ru по 1275 уникальных ключей, симметрическая разность 0, каждый из десяти пакетов `ui/i18n/*.js` — 1275 и совпадает с en. Прежние «1143/1217 против 791» больше не верны. Guide в `docs/` по-прежнему нет, и **автоматической проверки паритета в тестах нет** — `tests/test_i18n.py` её не содержит, так что расхождение может вернуться незамеченным |
| OSS06 draft release после успешных сборок | `external_blocker` | Windows не собирался и CI-job не выполнялся; macOS-артефакт отстаёт — 2.2.1 от 25.09 при 2.3.0. PyInstaller появился, так что macOS-часть блокировки снята, но черновик релиза без успешных сборок писать не на чем |
| OSS07 dependency/SBOM/checksums/provenance | `implemented_unverified` | `packaging/release_manifest.py`, `verify_release.py` — для macOS-сборки 2.2.1 |
| OSS08 публикация PyPI | `todo` | Не выполнялась (публикация вне scope) |
| OSS09 Homebrew/winget/Linux multiarch | `todo` | Не реализовано |
| OSS10 согласованные CONTRIBUTING/AGENTS/SECURITY/PRIVACY | `in_progress` | Документы есть; под `/v1`, менеджер ключей и новые флаги не обновлены |

---

## 7. X01–X08 и отвергнутые R-идеи (03-idea-catalog.ru.md)

| ID | Идея | Оценка | Статус | Чем закрыто |
| --- | --- | --- | --- | --- |
| X01 | Browser extension | Не реализовывалась: расширение добавляет разрешения магазина и политику браузера, а F17 закрывается существующими страницами, recipes и QR | `not_applicable_with_evidence` | `03-idea-catalog.ru.md:75`; F17 частично не закрыт (контроль маршрута, round-trip QR) — известная граница, а не обязательство написать extension |
| X02 | Удалённое управление | **Обязательная часть = F29**: отдельные identity, audit log, revocation/expiry, LAN opt-in | `implemented_unverified` | `functional_acceptance.py` подтвердил работу `/v1` с реальным ключом (30 `done` в этой сессии); менеджер ключей в интерфейсе проверен живым HTTP (§0.2); повторная выдача секрета почичена (§0.3). **LAN opt-in теперь доходит до сокета** (`gui.py:1335`) — но шлюз с `--lan` я не запускал, поэтому это доказательство по коду, а не наблюдение |
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
| 7 hostname/auth полный путь, смена пароля | `implemented_unverified` **(поднят с `in_progress`)** | `test_a_hostname_of_your_own_passes_the_whole_path`, `test_a_password_change_revokes_the_previous_proof`; **транспорт аутентификации появился в шлюзе** — `Proxy-Authorization: Basic` (RFC 7617) и логин/пароль для SOCKS5/5h, `gateway.py:293`, `secrets.py:813, 916`, 13 тестов `tests/test_area_gateway_credentials.py`. Не закрыто: измеряющая сторона по-прежнему отвергает пароль в URL (`probes.py:710`, `proxytool.py:138, 333`); живого соединения к прокси с паролем не было; OS-vault не проверен, `keyring` нет |
| 8 источники 200/304/429/partial, bad update не стирает last-good | `implemented_unverified` | Проверено на подставных `FeedResult`; живого fetch не было, и `source_feed` в живой базе не создаётся |
| 9 большой источник ограничен, первые результаты, stop/resume | `implemented_unverified` | Бюджеты и стадии работают (живой прогон §0.1, `E_LIMIT_BUDGET`); поток `collect → проба` отсутствует (F12) |
| 10 recheck/cancel/crash, timestamps, mixed-age, corrupt pointer | `verified` | Дефекты 2, 3, 4, 6, 9; соответствующие тесты зелёные |
| 11 экспорт выбранных не переключает активный пул | `verified` | Дефект 7; три теста |
| 12 empty/unsupported/expired не выбирает DIRECT | `implemented_unverified` | `test_an_empty_export_never_selects_a_direct_route`; клиентская валидация версии недостижима |
| 13 Maintained N восстанавливается, нехватка объясняется | `implemented_unverified` | `tests/test_pools_lifecycle.py` + живой `POST /api/pools/action refill` → 200 и `deficit_reason` в `GET /api/pools`; автономного watch нет |
| 14 gateway: concurrent max-per-proxy, partial handshake, cancel, shutdown | `verified` | `tests/test_gateway_reservation.py`, `test_gateway_bind.py`, `test_gateway_health.py` |
| 15 read-only и operator keys, scopes, revocation, SSE, quotas | `implemented_unverified` **(статус прежний, один дефект снят)** | Скоупы, разграничение и отзыв подтверждены приёмкой и живым HTTP §0.2. **Повторная выдача секрета по `Idempotency-Key` починена** — проверено вживую в этой сессии, §0.3. **Квоты параллелизма по-прежнему не работают:** `Principal.concurrency` записывается (`apiv1.py:547`) и больше нигде не читается, `QuotaGuard` (`apikeys.py:1497`) не имеет продуктового вызова |
| 16 полный API-сценарий внешним клиентом без GUI | `verified` | `test_one_bootstrap_key_finishes_the_whole_journey_without_the_gui`; функциональная приёмка прошла целиком через CLI+HTTP |
| 17 QR декодируется, recipes проверяемы, роли различаются | `in_progress` **(перепроверено, статус прежний)** | Рецепты есть; round-trip декодирование QR по-прежнему не проверяется — `tests/test_qr.py:14, 21` смотрит в исходник (`assertIn('upward = !upward', function)`, `class="qr-svg"`). Это единственный тест в репозитории, который проверяет исходный текст вместо поведения, и MASTER-PROMPT §7 это запрещает |
| 18 Windows/Mac запуск без Python, ресурсы, второй запуск, tray | `external_blocker` **(статус прежний, состав блокеров изменился)** | Собрать на macOS arm64 **теперь можно** — `PyInstaller` 6.22.3 установлен, helper меню-бара кладётся в bundle и spec без него прерывает сборку. **Не сделано:** ни одна пересборка на текущей ревизии; артефакты 2.2.1 от 25.09; подписи нет (`Signature=adhoc`, манифест `"signed": false`); Windows-job `windows-2022` не выполнялся ни разу — `gh run list --workflow windows.yml` → 404; Intel/universal2 не собираются (`SUPPORTED_ARCHES = ('arm64',)`) |
| 19 сон/смена сети/vault не повреждают историю | `in_progress` **(перепроверено, статус прежний)** | `desktop.py` вызывает `scheduler.mark_wake` и `jobs.mark_awake`; по отчёту владельца macOS-проверки пройдены — **я их не перепроверял**. Windows/Linux не проверялись. Интервальная сетка по-прежнему не переживает перезапуск: `db.py:781-783`, таблица `schedules` несёт 11 колонок и не содержит `last_run_at`, `paused`, `pause_reason`, `dst_policy`, `catch_up`, `wake_gap_s`; `SqliteScheduleStore.persists_runtime_state` остаётся `False` |
| 20 backup/migration/rollback/retention безопасны, канарейки не текут | `verified` | F24 + живое подтверждение: `python -m proxy_workbench backup` перечислил копии `v0`/`v14` с манифестами; `test_the_canary_secret_is_nowhere`. Схема выросла до 18, копия снимается и перед этим шагом тоже |
| 21 RU/EN, клавиатура, screen reader, 200% zoom, браузеры | `implemented_unverified` **(поднят с `in_progress`)** | **RU/EN закрыт полностью:** en и ru по 1275 уникальных ключей, симметрическая разность 0, все десять пакетов по 1275 и совпадают с en. **Клавиатура, screen reader, 200% zoom по-прежнему не проверялись** — автоматизация браузера в этой сессии недоступна. По коду: кольцо фокуса и `prefers-reduced-motion` есть, `role="progressbar"` нет, все 551 `font-size` в `px` без единого `rem` |
| 22 35 сценариев исследования сопоставлены | `verified` | Раздел 5: 28 карточек сопоставлены 1:1, X01–X08 и отвергнутые идеи — раздел 7 |

---

## 9. Сводка по F01–F29 и дефектам 1–26

| Статус | F01–F29 | Дефекты 1–26 | Всего (55) |
| --- | --- | --- | --- |
| `verified` | 4 — F02, F03, F11, F24 | 12 — 1, 2, 3, 4, 6, 7, 9, 10, 11, 13, 15, 16 | **16** |
| `implemented_unverified` | 23 | 11 — 5, 8, 12, 14, 17, 18, 19, 20, 21, 23, 24 | **34** |
| `in_progress` | 1 — F08 | 2 — 22, 25 | **3** |
| `todo` | 0 | 0 | **0** |
| `external_blocker` | 1 — F23 | 1 — 26 | **2** |
| `not_applicable_with_evidence` | 0 | 0 | 0 (встречается в X-матрице и DES14) |

**Число `verified` не сдвинулось: было 16, стало 16.** Это главный факт
сводки, и я не буду его смягчать. За волну ремонтов не закрылся ни один
направление до `verified`, потому что ни одно из них не было доведено до
наблюдения, а не потому, что ремонты были пустыми.

Что реально изменилось в распределении:

| ID | Было | Стало | Почему |
| --- | --- | --- | --- |
| F04 | `in_progress` | `implemented_unverified` | Транспорт аутентификации появился в шлюзе (RFC 7617, SOCKS5/5h), 13 тестов |
| F20 | `in_progress` | `implemented_unverified` | Падавший тест матрицы заменён на проверку, что измеренная возможность называет эндпоинт, бюджет и результат |
| дефект 14 | `in_progress` | `implemented_unverified` | Правка `reputation.py` из «в полёте» ушла в коммиты; прежние разворот IPv6 и правило «любой `127.*` = listed» в дереве отсутствуют |

Три ID выросли **не** потому, что зеленей стало больше тестов, а потому что у
каждого была конкретная причина низкого статуса, и эта причина снята
проверкой, которую можно повторить. Обратное тоже верно: **ни один ID не
понижен**, потому что прошлая сессия понизила POO05, API04 и API12, и я
перепроверил именно их:

- **API04** (health с причинами) был понижен за ложную тревогу `diagnose
  health`. Тревоги больше нет — `health: 5 checks, 1 problems`. Формулировка
  пользователю отдаётся верно. **Понижение снимаю**, статус возвращаю к
  прежнему `implemented_unverified` по остаткам: фильтры на legacy `/proxies`
  и на `/v1` всё ещё разные.
- **API12** (token scopes, rotation, revocation) был понижен за повторную
  выдачу секрета. Дефект починен и проверен вживую. **Понижение снимаю**,
  статус возвращается к прежнему `implemented_unverified` по остаткам:
  пер-ключевые квоты параллелизма не работают.
- **POO05** (бюджеты) остаётся пониженным: `--run-max-bytes` я числом не
  подтвердил, и в этом ничего не изменилось.

Ни одно из этих трёх понижений не было «по недоверию», и я это повторяю,
чтобы оно не выглядело как оправдание.

---


## 10. Что осталось непочиненным

Список получен **разбором кода на HEAD `d9056e5`**, а не принят на веру и не
перенесён из предыдущей версии документа: каждый пункт перепроверен, и пункты,
которые за волну ремонтов закрылись, отсюда убраны. Порядок — по тяжести.

### 10.1 Воспроизведённые дефекты

**Оба дефекта, которые предыдущая сессия воспроизвела вживую, починены.** Их
нет в этом списке; доказательства — в §0.3. Ниже то, что воспроизводится и
сейчас.

1. **`db.upsert_endpoint` спрашивает схему на каждый вызов.** `db.columns()`
   (`db.py:507-508`) не кэшируется; `upsert_endpoint` (`db.py:2221`) вызывает
   её на каждом вызове. Проверено на `d9056e5`:
   `grep -n "lru_cache\|_columns_cache\|COLUMN_CACHE" proxy_workbench/db.py`
   → 0 совпадений. Прежняя оценка (≈7 мкс на `columns()`, ≈33 мкс на весь
   `upsert_endpoint`) снята на прошлой сессии и с тех пор не изменилась.

### 10.2 Не подключено к пользовательскому пути

2. **F21 (сравнение источников и поставщиков) написано, но недостижимо.**
   `compare_sources`, `compare_suppliers`, `compare_cohorts`,
   `survival_across_windows` есть в `sourcedesk.py` и покрыты 43 тестами.
   Проверено на `d9056e5`: `grep -rn "compare_cohorts\|compare_sources\|
   compare_suppliers\|survival_across_windows" proxy_workbench/gui.py
   proxy_workbench/api.py proxy_workbench/apiv1.py proxy_workbench/proxytool.py
   proxy_workbench/ui/app.js` → **0 совпадений**.
3. **F22 в собранном приложении не проверен.** Helper меню-бара теперь
   кладётся в bundle и spec без него прерывает сборку — это видно в коде. Но
   **ни один артефакт на текущей ревизии ни собран, ни запущен**, поэтому
   утверждение «в `.app` меню-бар есть» остаётся утверждением о коде сборки, а
   не наблюдением. Windows и Linux не проверялись ни разу: ни `winreg`, ни
   loopback-канал, ни XDG autostart не запускались.
4. **`Workbench.filter_by_country` и `Workbench.country_criterion`
   не имеют вызывающих.** `proxytool.py:4338` и `:4351` определены;
   `grep -rn "filter_by_country" proxy_workbench/` даёт только определение, а
   `grep -rn "country_criterion" proxy_workbench/gui.py proxy_workbench/api.py
   proxy_workbench/apiv1.py` → 0 совпадений. API и GUI фильтруют страну своей
   строкой. Схема exit-модели по-прежнему не держит: `grep -n "hosting_basis\|
   exit_ip\|exit_country\|quota_basis" proxy_workbench/db.py` → 0 совпадений.
5. **Периодического watch пулов нет.** `pools.watch` определён
   (`pools.py:1360`) и в `__all__` (`:54`), но `grep -rn "pools.watch\|\.watch("
   proxy_workbench/*.py` → **0 совпадений**. Наполнение работает по явному
   действию (CLI, API, интерфейс), пул сам до N не восстанавливается. По той
   же причине `scheduler.py` не вызывает `scan`: автоматический запуск живого
   скана по расписанию я не наблюдал.
6. **Целевой клиент экспорта недостижим из интерфейса.** `--client-target` и
   `--client-binary` есть в CLI (`proxytool.py:4720, 4723`) и доходят до
   `ExportOptions`, а `exportsvc.resolve_client_target` (`exportsvc.py:1508`)
   подхватывает цель из окружения и `client.json` — это закрывает прежнюю
   претензию. Но `grep -n "client_target\|client_binary" proxy_workbench/gui.py`
   → **0 совпадений**, а в `api.py:1050` переменная с таким именем — это
   формат выгрузки, а не целевой клиент. Из GUI/API цель по-прежнему не
   выбрать. И **`sing-box` в окружении не установлен** (`which sing-box` →
   пусто), поэтому `client_check()` против настоящего бинарника не
   выполнялся ни разу — доказательства на фейковых исполняемых файлах.
7. **Измеряющая сторона не принимает прокси с паролем.** `probes.py:710`,
   `proxytool.py:138, 333` по-прежнему отвергают любой `username`/`password` в
   URL. Шлюз такие адреса довооружает (F04), но end-to-end путь «привезти свой
   прокси с паролем и измерить его» не открыт.

### 10.3 Ограничения, вшитые в архитектуру

8. **`collect` наполняет коллекцию до прогона.** Поток «источник → проба» есть
   внутри скана (`candidates()` читает коллекцию постранично и отдаёт байты в
   конвейер), но не между загрузкой и проверкой. Docstring
   `proxytool.py:2063-2071` признаёт это прямо.
9. **`--run-max-bytes` числом не подтверждён.** Он подключён в
   `chain.Budgets(max_bytes=…)`, но на фикстуре, где тело ответа не читается,
   счётчик остаётся `0` и потолок не срабатывает.
10. **Таблица `schedules` не переживает перезапуск.** `db.py:781-783`: 11
    колонок, нет `last_run_at`, `paused`, `pause_reason`, `dst_policy`,
    `catch_up`, `wake_gap_s`. Слияние слотов после сна работает внутри
    процесса, но интервальная сетка и накопленный бюджет после рестарта
    теряются. `SqliteScheduleStore.persists_runtime_state` остаётся `False`.
11. **Per-key `concurrency` и квоты не работают.** `Principal.concurrency`
    записывается (`apiv1.py:547`) и больше нигде не читается; `QuotaGuard`
    (`apikeys.py:1497`) не имеет ни одного продуктового вызова; единственный
    `ConcurrencyLimiter` пер-серверный.
12. **`allow_private` в `POST /v1/collections` и `PATCH` принимается и молча
    выбрасывается.** Живой HTTP: `POST /v1/collections
    {"name":"ap1","allow_private":True}` → **200**, а ответ
    `{"id","name","kind","created_at"}` — поля `allow_private` нет. Поле
    объявлено в OpenAPI (`apiv1.py:1179, 1185`), поэтому форма его предлагает.
    Смежное: имя, которое `api.py:1537` действительно читает,
    `allow_private_endpoints`, на этом маршруте отвергается как неизвестное
    (400 `E_VALIDATION_UNKNOWN_FIELD`) — оно относится к импорту, где
    политика теперь настоящая и пишется в аудит.
13. **Живая лента в UI по-прежнему не из хранилища измерений.** `app.js:4622`
    читает `/api/events`, а `gui.py:117` `EVENTS_FILE = 'gui-events.jsonl'`;
    `JobStore._emit` из `store()` в `proxytool.py` не вызывается. Сторона
    `jobs.py` пишет реальные события в `job_event` по одному на item, и они
    никуда не показываются.
14. **Ни одного живого fetch источников, 304 или last-good.** `sourcedesk.py`
    это умеет и покрыто тестами на подставных `FeedResult`; вживую я не
    выполнял ни одного, потому что сеть запрещена. Это же относится к WS,
    длительному соединению и media (F20) и к DNSBL (дефект 14): код есть,
    наблюдения нет.
15. **`country_resolver` не читает metadata текущего прогона.**
    `proxytool.py:3718`, вызывается на `:6390` до сбора, поэтому гео после
    `collect` в разрешении стран не участвует.
16. **Каталог источников по-прежнему указывает на `main/sources.json`.**
    `branding.py:18`.

### 10.4 Доступность и внешний вид

17. **Мелкий текст — находка доступности, а не вкусовое.** Разбор всех правил
    `proxy_workbench/ui/style.css`: **551 объявление `font-size`, из них
    1×8.5px (`.gauge-caption`), 6×9px (`.gauge-caption`, `.ticker-badge`,
    `.mac-terminal-badge`, `.code-editor-badge`, `.target-status-label`,
    `.scenario-tag` в одной сетке), 13×9.5px (`.scenario-tag`, `.view-count`,
    `.api-method-tag`, `.freshness-badge`, `.live-badge`, `.ticker-code`),
    41×10px, 42×10.5px** — то есть **менее 11px на 103 правилах**. Хуже
    всего то, что **все 551 объявление записаны в `px`, `rem` — ноль**, и
    пользовательской настройки размера текста в приложении нет. При 100% на
    обычном мониторе 9px — это примерно 6.75pt; 8.5px — примерно 6.4pt. Это
    ниже того, что считают комфортным минимумом, и заметно ниже 11pt, который
    даёт браузерное увеличение текста. Ничего из этого нельзя исправить
    настройкой ОС: браузерный zoom масштабирует `px` целиком, но не даёт
    увеличить именно мелкий текст отдельно от раскладки.
18. **Что проверено глазами, а что — нет.** Владелец ремонта открывал страницу
    в браузере и кликал: страницы открываются, вёрстка без горизонтальной
    прокрутки, подписи на месте, кнопки каталога работают (именно так и был
    найден мёртвый `setupCatalogListeners`). **Я эти проверки не повторял** —
    автоматизация браузера в этой сессии недоступна. Чего конкретно глазами
    **не** смотрели: реальные цвета и тёмная тема (в `style.css` есть
    `[data-theme="light"]`, переключатель `app.js:6972-6980`, но контраст
    никто не измерял); поведение фокуса при настоящем нажатии Tab; 200% zoom;
    внешний вид меню-бара. Всё это остаётся непроверенным, и я не буду
    выдавать код `:focus-visible` за проверенное поведение.
19. **Фокус в коде есть, поведение не проверено.** Глобальное правило
    `style.css:9335-9339` даёт кольцо 2px, 75 правил с `:focus`, а там, где
    `outline` снят (`.button:focus-visible` `:11947`, `select:focus-visible`
    `:13474`, `.code-editor-input` `:17636` и ещё около 20), кольцо заменено на
    `box-shadow`. То есть авторы кольцо сознательно заменили, а не забыли.
    Но `prefers-reduced-motion` (22 вхождения) и кольцо фокуса — это наличие
    стиля, а не подтверждение, что при Tab пользователь что-то видит.
20. **`role="progressbar"` в `index.html` — 0 совпадений.** Полосы воронки
    размечены `role="img"` с `aria-label` (`app.js:9788`), то есть это подпись
    с картинкой, а не прогресс-роль. `aria-live` присутствует (4 вхождения).
    UX11 остаётся `todo` по существу.

### 10.5 Внешние блокеры

21. **Подпись и нотаризация macOS.** Ключей нет, не создавались и не
    покупались. Проверено: `codesign -dv "/tmp/pw-dist/Proxy Workbench.app"` →
    `Signature=adhoc`; манифест отдаёт `"signed": false`,
    `"reason": "no signature was found"`, а `"notarization": {"available":
    false}`. `packaging/release_manifest.py` ставит флаг только из
    `desktop.signature_of`, который возвращает `True` лишь после того, как
    инструмент подписи реально проверил файл. Это `external_blocker`, а не
    «осталось нажать кнопку».
22. **Windows: сборщик и spec доведены, артефакт не собран ни разу.**
    `build_windows.py` переписан в волне `8be591b` (211 строк), `windows.yml`
    описывает job `windows-artifact` на `windows-2022`, который читает
    PE-подсистему обоих бинарников, стартует GUI-версию без Python, проверяет
    второй запуск, компилирует per-user установщик и сверяет манифест. **Ни
    разу не выполнялся:** `gh run list --workflow windows.yml` →
    `HTTP 404: workflow windows.yml not found on the default branch`; workflow
    существует только в этой ветке. Локально подтвердить нечем: машина — macOS
    arm64, `iscc` не установлен. Это `external_blocker`.
23. **Intel/universal2 на macOS не собираются.** `build_macos.py:38`
    `SUPPORTED_ARCHES = ('arm64',)`; `:86-90` и `:422` отказывают с честным
    объяснением, что universal2 требует двух собранных и слитых срезов.
    PyInstaller теперь есть, но Intel-срез на arm64-машине собрать нечем.
24. **Артефакты macOS отстают.** `/tmp/pw-dist/` — версия **2.2.1 от
    25.09 21:49** при текущей `2.3.0`. **Важное изменение против прошлой версии
    этого документа: `PyInstaller` 6.22.3 теперь установлен в `.venv`**, то
    есть прежний подблокер «пересборка невозможна без сети» снят. Остаётся
    факт: пересборку на текущей ревизии не делал никто.

### 10.6 Документация, которая отстала

25. **`SECURITY.md` и `CONTRIBUTING.md` не переписаны под `/v1`.**
    `grep -c "api-key\|/v1/" SECURITY.md CONTRIBUTING.md` → **0** в обоих.
26. **`tests/test_qr.py` проверяет исходный текст, а не поведение.** Строки 14
    и 21 ищут `upward = !upward` и `class="qr-svg"` в исходнике. Round-trip
    декодирование QR не проверяется ни одним тестом; MASTER-PROMPT §7 это
    запрещает, R20 отмечает три цикла подряд.
27. **В README осталось противоречие про `--workers`.** `README.md:257`
    правильно объясняет, что это потолок, но `README.md:421` по-прежнему
    говорит «fail-fast, 128 workers» и «Raise `--workers`» в расчёте времени.
28. **Паритет переводов закрыт, но ничем не защищён.** en/ru по 1275 ключей,
    десять пакетов по 1275, всё совпадает. Но `tests/test_i18n.py` проверяет
    определение языка, кодировку и перевод CLI-строк, **а не паритет ключей**,
    и `grep -c "lru_cache" tests/test_i18n.py` → 0. Расхождение может вернуться
    и никто не заметит. Guide для новых языков в `docs/` тоже нет.

---

**Что этот документ не утверждает.** `Ran 3249 tests … OK (skipped=1)`
показывает согласованность дерева, а не полноту функций. **Более того: до
`8e24d23` этот же прогон оставался зелёным, пока ни одна кнопка в браузере не
работала** — потому что ни один тест не смотрел на `app.js`. Теперь смотрит
`tests/test_web_bundle_init.py`, и он падает при возврате бага; это шаг вперёд,
но browser E2E по-прежнему нет.

Проверки этой сессии доказывают конкретные вещи: (1) дерево согласовано и не
падает — 3249 тестов, код выхода 0; (2) интерфейс **загружается** и обработчики
прикрепляются — новый тест это проверяет и проверен в обе стороны; (3) два
дефекта, которые прошлая сессия воспроизвела вживую, починены и перепроверены
вживую; (4) диагностика, фоновый слой, менеджер ключей, импорт, пулы и
расписания достижимы снаружи по HTTP; (5) переводы на полном паритете, схема БД
на 18.

Они **не** доказывают: F21 в пользовательском пути, меню-бар в собранном
`.app` (ни один артефакт не собран), измерение прокси с паролем, клиентскую
валидацию экспортов настоящим бинарником, Windows-поставку, подпись,
доступность при реальном Tab и 200% zoom. Скорость и качество живых публичных
прокси не измерены и не оценивались — таких измерений в этом документе нет.
