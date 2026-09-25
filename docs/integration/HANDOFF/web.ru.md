# Handoff: web (поверхность интерфейса)

**Требования:** F17, F19; дефекты 3, 5, 21, 23, 24, 25. Сопутствующие: дефекты 7, 8, 9, 12, 18, R02, R03, R04, R15, R17, R18.
**Контракт:** `docs/integration/CONTRACTS.ru.md` §2.2, §2.3, §2.4, §4.3, §4.5, §5.1, §5.4, §5.6, §5.7, §6.4 (версия 1).
**База:** ветка `integration/ultra-2026-09-25`, рабочее дерево с незакоммиченными правками пользователя в `proxy_workbench/ui/*` (дизайн — его база, я их сохранил и не переписывал).
**Владею:** `proxy_workbench/gui.py`, `proxy_workbench/ui/app.js`, `proxy_workbench/ui/index.html`, `proxy_workbench/ui/style.css`, `proxy_workbench/i18n.py`.

## 0. Что изменено в этом проходе (2026-09-26)

Первый проход уже закрыл F17/F19 и дефекты 3, 5, 21, 23, 24, 25 на уровне сервера и основной механики страницы. Аудит требований построчно против `MASTER-PROMPT.ru.md` §3–§4 и `REVIEW.ru.md` R02/R03/R15/R17/R18 нашёл пять незакрытых мест; все пять закрыты в этом проходе:

| Находка | Где было | Что сделано | Тест |
| --- | --- | --- | --- |
| **F17 «точные поля»**: шаг 3 пути подключения, вкладка браузера и рецепты cURL/Python содержали литерал `127.0.0.1:8899`. При `--gateway-port N` или `--no-gateway` страница обещает несуществующий адрес, а рецепт копируется в терминал как есть | `index.html` (карточка пути и вкладки), `app.js` `renderConnectPath` | Поля адреса/порта/протокола, текст шага 2, вкладка браузера и оба скриптовых рецепта заполняются из живого `gateway.address`. IPv6-литерал берётся в скобки. Выключенный шлюз показывает «—», а не прошлый адрес | `tests.test_web_connect.ConnectionPathRenderTests` (6 тестов) |
| **Дефект 3 на поверхности**: сервер различает `all_expired` и `empty_no_match`, но страница показывала это значение машиной строкой в пути подключения и вообще не показывала его в списке результатов | `app.js` `renderConnectPath`, `renderViewTabs`; `gui.py` `gateway_state`, `results`; `i18n.py` | Добавлен каталог `i18n.STATE_DETAILS` (все значения `core.STATE_DETAILS` + два состояния reader'а) и `i18n.state_detail_text()`. Сервер отдаёт `state_detail_label` рядом с кодом, страница показывает причину словами и сохраняет код | `tests.test_web_readpath` (`test_the_reason_travels_to_the_page_in_words_and_as_a_code`), `tests.test_web_browser.ResultListClientTests.test_the_page_says_why_the_list_is_what_it_is`, `tests.test_web_i18n.StateDetailTranslationTests` (4 теста) |
| **Дефект 23 «полная перепроверка предлагается отдельно»**: сервер отдавал `scope.recheck_action` и `recheck_note`, перевод `quick.recheckFull` существовал — но страница ничего не предлагала. Ключ был мёртвым | `app.js` `renderResults`, новая `offerFullRecheck`, делегированный обработчик `[data-recheck-proxy]`, `bulkAction(extra.proxies)`; `style.css` `.recheck-btn` | После быстрого теста рядом с ответом появляется чип «Полная перепроверка» с формулировкой сервера; он предлагается только когда сервер его прислал, не дублируется и шлёт ровно ту строку, по которой кликнули, а не весь чекбокс-выбор | `tests.test_web_browser.QuickTestRecheckTests` (4 теста) |
| **Дефект 24 / R17 «versioned profile presets»**: у сценариев не было версии определения | `app.js` `SCENARIOS`, карточка сценария | У каждого сценария `version`, применённый сценарий показывает `Preset v1` / `Пресет v1` | `tests.test_web_browser.ScenarioPresetTests.test_every_preset_is_versioned` |
| **Дефект 21**: при обрыве передачи `pipeTo` бросал, поток оставался открытым, а пользователь получал файл, дописанный наполовину | `app.js` `downloadFile` | Ошибка передачи вызывает `writable.abort()` — единственное закрытие на этом пути; `close()` не вызывается, пользователь видит сообщение, кнопка остаётся рабочей | `tests.test_web_browser.DownloadTests.test_a_failed_transfer_aborts_instead_of_closing_and_leaving_a_half_file` |

Ничего из этого не меняло layout, темы, цвета и визуальный язык: добавлены один класс кнопки (`.recheck-btn`, копия существующего `.quick-test-btn`), четыре перевода и заполнение уже существовавших блоков. Дефекты 5, 25 и основа F19 в этом проходе не менялись — они закрыты первым проходом и перепроверены его же тестами (все зелёные, см. §4).

---

## 1. Прошу внести в чужие файлы

### 1.1 `proxy_workbench/proxytool.py` — интегратор

1. **`store()` (`proxytool.py:1380-1391`) — события измерений.** Сейчас живую ленту интерфейса я строю сам: `App.collect_events()` читает таблицу `results` по `checked_at` и превращает каждое завершённое измерение в событие (`item.observation`, `code`, `data.proxy/latency_ms/reliability/error`) в `data/gui-events.jsonl`, курсор — `(stream_id, seq)` по §5.7. Это честный поток измерений, но источник — хвост таблицы, а не журнал задания.
   **Прошу:** после записи измерения в `store()` вызывать `JobStore._emit(job_id, 'item.observation', code, data)` (`jobs.py:607`). Тогда `/api/events` в `gui.py:1952` переключается на `JobStore.events(job_id, after_seq=...)` — API уже умеет отдавать SSE по той же схеме курсора, и лента, API и `/v1/events` читают один поток.
   Пока это не сделано, события ленты живут в `data/gui-events.jsonl`; файл очищается в `App.clear_data()`.

2. **`export()` (`proxytool.py:1651-1932`) — дефект 7, `kind='selection'`.** Кнопки «Download Selected» и «Download scope» идут у меня через `POST /api/results/bulk` → `App.start({'action': 'export', 'selection': [...]})` (`gui.py:665`). Это по-прежнему публикующая публикация, поэтому таблица и пул всё ещё переключаются. Прошу реализовать `kind='selection'` по §4.5: отдельный артефакт, `current.json` и `last-profile.txt` не трогаются. Пункт приёмки F19 «выделенное не меняет активный пул» закрывается только на вашей стороне.

3. **`run_scan` + watch (`proxytool.py:2464-2511`) — дефект 12 и §1.2 (4).** `App.start()` пишет `scope` в `gui-job.json` (`gui.py:786`) — `action`, `selection_requested`, цели, профиль запроса. Прошу добавить в этот файл `scope_digest` и `input_digest`, а в `resume` — запрет расширения scope (§6.2).

4. **Порог допуска из ревизии профиля, а не из аргумента чтения (§2.3).** `App.result_policy()` (`gui.py:1011`) собирает один `core.Policy` и зовёт `core.admit` для каждой строки — это тот же контракт, что у `api.select`. Но `min_success`, `max_latency`, `countries`, `min_anonymity` пока приходят из query string (их задаёт форма), потому что так работала старая таблица. Прошу, когда `profiles.py` будет подключён, брать эти значения из `profile_revision`, а query-параметры трактовать как сужение в пределах ревизии. Тогда `tests.test_parity` (сравнение `api.select` / CLI / `App.results`) станет достижим.

5. **`max_age_seconds` в снимке.** `App.snapshot_policy()` (`gui.py:459`) читает `status['max_age_seconds']`; при отсутствии берёт `core.DEFAULT_MAX_AGE_SECONDS`. Как только `export()` начнёт писать `max_age_seconds` (§4.3), интерфейс подхватит это без правок.

### 1.2 `proxy_workbench/api.py` — интегратор

6. **`api.py:181-182` — дефект 3 в API и шлюзе.** `Exports.load()` обнуляет `rows`, когда истёк общий `status['valid_until']`. Я перестал использовать `Exports.load()` в своей таблице и читаю поколение сам (`gui.py:read_snapshot`, `App.export_status` — построчная свежесть через `core.select`), поэтому интерфейс дефект 3 не воспроизводит: смешанный набор даёт `state='partial'`, `expired_count=1`, `stale=False`, и свежие строки остаются в таблице. **Прошу то же самое сделать в `Exports` и в шлюзе** — иначе API и GUI будут описывать одно и то же поколение разным числом строк, а это ровно тот разрыв, который запрещает §2.3.

7. **Дефект 8 — фиксация поколения у потребителя (§1.2 (3)).** `App.snapshot()` (`gui.py:read_snapshot`) фиксирует поколение один раз на запрос и больше указатель не перечитывает; `/api/download/*` открывает файл именно этой generation (`gui.py:1968`). В API и шлюзе нужно то же: `generation` в курсоре/состоянии, а не глобальный `current.json` на каждое чтение.

### 1.3 `proxy_workbench/gateway.py` — поверхность `gateway`

8. **Контроль маршрута (F17).** Сейчас страница «Путь подключения» говорит правду, но показать, какой именно прокси обслужил последнее соединение, и закрепить выбранный адрес она не может: у `gateway.Gateway` (`gateway.py:320`) и `Pool` (`gateway.py:85`) нет такого поля. **Прошу** добавить в снимок пула `last_proxy` и `last_used_at` и (по возможности) `pin`/`unpin` на адрес, чтобы шаг «4. Контроль маршрута» был не только текстом. Место для показа уже готово: `gui.py:gateway_state()` возвращает `binding`, страница рисует его в `#connect-generation` и `#connect-pool`.
   Пункт «понятное отключение» закрыт на моей стороне: `POST /api/gateway/stop` вызывает существующий `Background.close()` (`gateway.py:558`).

9. **`Pool.snapshot()` — generation.** `App.gateway_state()` (`gui.py:565`) берёт generation из `App.export_status()`, то есть из публикации, а не из того снимка, который реально держит шлюз. Прошу вернуть в `Pool.snapshot()` имя поколения и `expires_at`, чтобы страница показывала binding пула, а не binding публикации.

### 1.4 `proxy_workbench/maintenance.py` — исполнитель `desktop.py`

10. **Runtime-файл `gui-events.jsonl`.** Поток событий измерений — машинные runtime-данные, и `App.clear_data()` (`gui.py:876`) удаляет его сам. Прошу добавить `EVENTS_FILE = 'gui-events.jsonl'` в `RUNTIME_FILES`, чтобы очистка из CLI совпадала с очисткой из интерфейса.
    Чего делать **не** надо: `gui-annotations.json`, `gui-views.json`, `gui-history.json` — пользовательские документы (теги, избранное, заметки, представления, история отмен). Они переживают очистку, как настройки и denylist, и в `RUNTIME_FILES` не попадают.

### 1.5 `proxy_workbench/core.py` / `jobs.py`

11. Ничего не прошу. `core.admit`, `core.select`, `core.Policy` и `core.Scope` я использую как есть, ничего не дублирую; собственный `freshness_of()` (`gui.py:158`) — это раскладка уже вынесенного `Admission` по четырём представлениям интерфейса, а не второй контракт допуска.

---

## 2. Что уже сделано у меня (публичный API, на который это опирается)

Всё в `gui.py`; список маршрутов и поведение описаны в `README` этого handoff ниже.

| Что | Где | Что даёт потребителям |
| --- | --- | --- |
| `read_snapshot(data)` → `Snapshot` | `gui.py:215` | Поколение, прочитанное без writer-lock; `state ∈ {ok, legacy, broken, missing}`. `broken` никогда не откатывается к строкам БД (дефект 9). |
| `App.export_status()` | `gui.py:459` | Статус поколения с построчной свежестью: `state`, `state_detail`, `available`, `expired_count`, `expires_at`, `stale` только когда истекли **все**. |
| `App.result_plan(query)` | `gui.py:978` | Валидированный план области + `scope_digest`. От него зависят таблица, матрица и любая массовая операция. |
| `App.results(query)` | `gui.py:1165` | Страница строк: `rows`, `total`, `counts`, `view`, `scope_digest`, `columns`, `snapshot_state`. `view ∈ fresh/stale/failed/unknown/all`. |
| `App.matrix(query)` | `gui.py:1205` | Матрица proxy × target по текущей странице. |
| `App.bulk(payload)` | `gui.py:1609` | `op ∈ recheck/export/copy/tag/untag/note/favorite/unfavorite/exclude/include/denylist`, `scope ∈ page/selected/all_matching`. Для `all_matching` браузер шлёт фильтр, а не строки. |
| `App.test_proxy(payload)` | `gui.py:1296` | Ответ с `scope` (объём клика, что не измерялось, `whole_deadline_s`, `recheck_action`) и `stored=False`. |
| `App.events(query)` | `gui.py:1812` | События измерений с курсором `stream:seq`, `source='measurements'`. |
| `App.gateway_state()` | `gui.py:565` | `address`, `binding` (включая `state_detail_label`), `transport`, `probe_note`, `disconnect_hint`; **никогда** `app.token`. |
| `App.stop_gateway()` | `gui.py:604` | Понятное отключение: слушатель действительно останавливается. |
| `i18n.code_text(code, lang)`, `i18n.code_lines(*codes)` | `i18n.py:84` | Перевод канонических кодов; неизвестный код возвращается как есть. |
| `i18n.state_detail_text(detail, lang)` | `i18n.py:117` | Перевод `state_detail` (§4.3): покрывает всё множество `core.STATE_DETAILS` плюс `no_snapshot` и `pointer_unreadable`, которые добавляет сам интерфейс. Неизвестное значение возвращается как есть. |

Маршруты: `GET /api/results`, `/api/results/matrix`, `/api/result-detail`, `/api/events`, `/api/annotations`, `/api/views`, `/api/history`, `/api/download/*`; `POST /api/results/bulk`, `/api/views`, `/api/views/delete`, `/api/history/undo`, `/api/gateway/stop`, `/api/test-proxy` (прежний), `/api/denylist/add` (прежний).

Изменения поведения, которые важны интегратору:

* **дефект 5:** `App.results`, `App.detail`, `/api/download/*`, `/api/results/matrix` и `/api/events` больше **не** берут `data_lock()`. Чтение идёт по WAL в read-only соединении; поколение фиксируется один раз на запрос.
* **дефект 23:** быстрый тест получил общий срок `QUICK_TEST_DEADLINE_S = 45 c` поверх `asyncio.wait_for`, вердикт считается по общей политике (denylist, порог, strict, требуемая анонимность) и **не может** вернуть `ok=True`, если требуемая анонимность не измерялась: код `E_STATE_ANONYMITY`. Наблюдение не сохраняется; полная перепроверка — отдельное действие `recheck`.
* **дефект 24/25 (клиент):** живая лента больше не ищет `OK|PASS|FAIL` в агрегированном логе; сценарии применяют полный набор полей после сброса (`SCENARIO_FIELDS` + `resetScenarioFields()`), а названия не обещают звонки, 4K и весь сервис.
* **дефект 3 (этот проход):** `App.export_status()` и `App.results()` отдают `state_detail` (код) и `state_detail_label` (перевод) рядом. Страница показывает причину словами в подсказке над таблицей и в пути подключения, сохраняя код, — «ничего не подошло» и «всё истекло» больше не выглядят одинаково. Каталог переводов один, в `i18n.py`; страница ничего не переводит сама.
* **F17 (этот проход):** адрес, порт и протокол в шагах 2–3, во вкладке «браузер» и в рецептах cURL/Python берутся из живого слушателя (`gateway.address`), а не из литерала в разметке. В статической разметке остался только пример с настоящим значением `gateway.DEFAULT_PORT`, который перезаписывается первым же опросом состояния.
* **дефект 18 (часть):** пароль шлюза — отдельный секрет `App.gateway_token`, `--gateway-host` по умолчанию `127.0.0.1`, LAN только через явный `--lan`.
* **F19 (клиент):** представления, области, массовые операции, теги, избранное, заметки, сохранённые представления, колонки (компактные по умолчанию), матрица, история с отменой, выборка сбрасывается при смене `scope_digest`.

---

## 3. Совместимость

**Что ломается, если не внести пункты 1.1 (2) и 1.2 (6).** «Download Selected» продолжит переключать активный пул (дефект 7), а `/proxies` в API продолжит отдавать пустой список для смешанного набора, пока таблица интерфейса показывает свежие строки. Сценарий из REVIEW R04 («в пуле 100 адресов → выделить 2 → выдача сократилась») воспроизводится до интеграции пункта 1.1 (2); после него воспроизводиться не должен.

**Что НЕ ломается:**

* Существующие маршруты `/api/results`, `/api/result-detail`, `/api/download/*`, `/api/test-proxy`, `/api/denylist/add` сохранены; `view` и `limit` — необязательные параметры, поэтому старые вызовы без них работают как раньше (по умолчанию `view=fresh`, что совпадает со старым `row_fresh`-фильтром).
* `min_success`, `quick`, `hosting`, `country`, `q`, `offset`, `sort` обрабатываются как прежде; неизвестные значения отвергаются с 400, а не игнорируются.
* `gui.CHILD_ENV['PROXY_WORKBENCH_LANG'] == 'ru'` не тронут (проверяется в `tests/test_web_i18n.py`).
* Дизайн пользователя сохранён: правки в `ui/*` — только новые блоки (представления, области, матрица, история, путь подключения, источник живой ленты) плюс честные тексты сценариев; темы, layout и стили не переписывались. Во втором проходе добавлен один класс кнопки (`.recheck-btn` — копия существующего `.quick-test-btn`) и заполнение уже существовавших блоков; ни один существующий селектор не переименован и не удалён.
* `index.html`: `id` новых элементов — `connect-client-hint`, `connect-host`, `connect-port`, `connect-protocol`, `gw-browser-host`, `gw-browser-port`, `gw-browser-protocol`, `gw-curl-code`, `gw-python-code`, `gw-copy-curl`, `gw-copy-python`. Проверено, что дубликатов `id` в странице нет и что все `data-i18n` ключи страницы присутствуют в обоих каталогах.
* Данные: три новых sidecar-файла в `data/` (`gui-annotations.json`, `gui-views.json`, `gui-history.json`) — пользовательские документы, не результаты измерений; при их отсутствии всё работает как раньше.

**Про F19 и `ui/views/*.js`.** `HANDOFF/README.ru.md` §1.4 выделял F19 отдельному исполнителю с файловым доменом `proxy_workbench/ui/views/*.js`. Мне F19 назначен на `gui.py` + `ui/*`, поэтому я сделал его в существующих файлах и **не создавал** `ui/views/`. Если интегратор всё же запустит исполнителя `ui-views`, его область нужно закрыть: перенос уже сделанного перепишет работающий код.

---

## 4. Проверки

Все команды — из корня репозитория, `.venv/bin/python -m unittest tests.test_web_<модуль>`. Полный `discover -s tests` не запускался: по условию его выполняет интегратор, и в это время он может быть красным из-за чужих незаконченных модулей.

### Прогон второго прохода (2026-09-26), фактический вывод

| Команда | Результат |
| --- | --- |
| `.venv/bin/python -m unittest tests.test_web_readpath` | `Ran 8 tests in 5.637s` — `OK` |
| `.venv/bin/python -m unittest tests.test_web_results` | `Ran 22 tests in 11.345s` — `OK` |
| `.venv/bin/python -m unittest tests.test_web_quicktest` | `Ran 9 tests in 4.585s` — `OK` |
| `.venv/bin/python -m unittest tests.test_web_events` | `Ran 7 tests in 3.600s` — `OK` |
| `.venv/bin/python -m unittest tests.test_web_browser` | `Ran 22 tests in 0.978s` — `OK` |
| `.venv/bin/python -m unittest tests.test_web_connect` | `Ran 24 tests in 4.677s` — `OK` |
| `.venv/bin/python -m unittest tests.test_web_i18n` | `Ran 12 tests in 0.011s` — `OK` |
| `node --check proxy_workbench/ui/app.js` | без вывода, код 0 |
| `.venv/bin/python -m py_compile proxy_workbench/gui.py proxy_workbench/i18n.py` | без вывода, код 0 |

Смежные наборы, которые читают те же файлы (запущены, чтобы убедиться, что правка ничего у них не сломала; это не мои тесты, и полный набор они не заменяют): `tests.test_gui` — `Ran 24 tests in 12.892s`, `OK`; `tests.test_i18n` — `Ran 3 tests`, `OK`; `tests.test_qr` — `Ran 1 test`, `OK`; `tests.test_selection` — `Ran 17 tests in 1.360s`, `OK`.

Проверки целостности страницы (скрипт на Python, читает только файлы): дубликатов `id` в `index.html` — 0; `data-i18n` ключей страницы, отсутствующих в одном из каталогов, — 0.

Живой smoke маршрутов на временной папке: `GET /` → 200, в отданной странице присутствуют `id="connect-host"` и класс `.recheck-btn` в `style.css`; `GET /api/results?view=all` → `state_detail=ok`, `state_detail_label='the set was built and accepted'`; `GET /api/state` отдаёт 15 колонок в `all_columns`. `POST /api/results/bulk` с `op=recheck` и пустыми `settings` вернул 400 с текстом «Выбранные адреса могут экспортироваться…» — это существующая валидация `App.start`, а не регрессия; рабочий путь recheck с сохранёнными настройками закрыт `tests.test_web_results.test_bulk_recheck_starts_a_job_for_the_whole_scope`.

### Прогон первого прохода (2026-09-25), зафиксированный ранее

| Команда | Результат в той сессии |
| --- | --- |
| `.venv/bin/python -m unittest tests.test_web_readpath` | `Ran 7 tests … OK` — дефекты 5 и 3 |
| `.venv/bin/python -m unittest tests.test_web_results` | `Ran 22 tests … OK` — F19 на сервере |
| `.venv/bin/python -m unittest tests.test_web_quicktest` | `Ran 9 tests … OK` — дефект 23 |
| `.venv/bin/python -m unittest tests.test_web_events` | `Ran 7 tests … OK` — дефект 25 (источник событий) |
| `.venv/bin/python -m unittest tests.test_web_browser` | `Ran 15 tests … OK` — дефекты 21, 24, 25 (клиент), F19 (клиент), под node |
| `.venv/bin/python -m unittest tests.test_web_connect` | `Ran 16 tests … OK` — F17 и QR round-trip |
| `.venv/bin/python -m unittest tests.test_web_i18n` | `Ran 8 tests … OK` — коды и каталоги переводов |

**QR проверяется настоящим декодированием.** `tests/web_support.py` содержит свой декодер QR: он читает SVG, восстанавливает карту функциональных модулей в том же порядке, что и кодировщик, проверяет форматную информацию (BCH + маска 0x5412), снимает маску, идёт зигзагом, де-интерливит блоки и проверяет синдромы Рида — Соломона, после чего разбирает байтовый режим и возвращает payload. `test_a_qr_decodes_back_to_the_same_link`, `test_an_ipv6_host_is_encoded_and_decoded`, `test_the_page_escapes_an_ipv6_address_for_the_telegram_link` и `test_the_qr_never_carries_the_gui_or_api_secret` проходят именно через декодирование, а не по наличию строк в SVG.

**Что осталось непроверенным (честно).**

* Полного browser E2E (Chromium, реальные клики, save-picker) не выполнялось: в этом окружении нет браузерного драйвера, поэтому `defect 21` проверен по поведению функций под node со стабами `showSaveFilePicker`/`fetch`/`pipeTo` (активация до первого `await`, `preventClose: true`, ровно один `close()` или один `abort()`, `AbortError` без тоста, fallback через anchor), а не настоящим кликом в Chromium. R15 поэтому закрыт **на уровне кода страницы**, но не воспроизведён в браузере.
* Внешний вид новых элементов (чип перепроверки, заполняемые поля пути подключения) в реальном браузере не снимался: сверка велась по DOM-контракту, а не по скриншоту. Класс `.recheck-btn` скопирован с `.quick-test-btn`, размеры и цвета те же.
* Совместимость `/api/results` с `api.select` не проверялась: пункт 1.1 (4) ещё не сделан, и `tests.test_parity` авторства интегратора сейчас был бы красным.
* `singbox.yaml`/`clash.yaml` из пути подключения не валидировались целевым клиентом (дефект 20, владелец `exportsvc.py`).
* `docs/requirements/product-research/` и `sources-research/` я не перечитывал построчно: требования F17/F19 и дефекты брал из MASTER-PROMPT §3–§4 и REVIEW R02/R03/R15/R17/R18, как указано в задании.

---

## 5. Открытые вопросы

1. **Дефект 7 и F19 «выделенное не меняет активный пул»**: пока `export` публикует поколение, пункт приёмки F19 не закрыт. Нужен `kind='selection'` от интегратора; я не могу закрыть это в `gui.py`, не сломав `proxytool.py`, который мне не принадлежит.
2. **Контроль маршрута (F17)**: без `last_proxy`/`pin` в `gateway.py` шаг 4 остаётся текстом. Если `gateway` не расширит снимок пула, я могу показать только привязку к публикации — и тогда пункт F17 «контроль маршрута» будет закрыт частично; это нужно решить владельцу `gateway.py`.
3. **Доступ (`access_revision`)**: `F04` и приёмка F09 «новая версия доступа одинаково влияет на GUI/API/gateway» остаются внешним блокером (§8 п.2 CONTRACTS). В `gui.py` я читаю строки с `allow_missing_identity=True`, потому что колонок `access_id`/`access_revision` в `results` ещё нет; после миграций 5 и 13 это значение должно перестать быть допустимым.
4. **Трансляция кодов**: `i18n.CODES` закрывает все коды из `core.REASON_CODES`; `i18n.STATE_DETAILS` закрывает всё множество `core.STATE_DETAILS`. Новые коды из `diagnostics.py`/`jobs.py` и новые значения `state_detail` из `core.py` нужно дописывать туда же — иначе страница покажет код без текста (это допустимо и не ломает контракт, но стоит держать в одном месте). Тест `tests.test_web_i18n.StateDetailTranslationTests` упадёт, если `core.STATE_DETAILS` пополнится без перевода.
