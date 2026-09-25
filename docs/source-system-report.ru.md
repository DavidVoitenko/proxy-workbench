# Система источников: что сделано и что проверено

**Отчёт по:** рабочей копии `/Users/main/Desktop/111/proxy-workbench-sources`, ветка `sources-catalog`
**Дата отчёта:** 2026-09-25
**Базовая точка сравнения:** коммит `806d13d` («Baseline: import in-progress parallel work from the main checkout»), `git rev-parse HEAD` = `806d13dbdabaf3906858efd4d3b40bcf0f945d19`
**Статус:** не релиз. Ничего не опубликовано, коммитов не делалось, push не выполнялся. Изменения лежат незакоммиченными в рабочем дереве.

Все числа ниже получены командами, приведёнными рядом с ними, в этой копии, на 2026-09-25. Всё, что не удалось выполнить или проверить, помечено прямо в тексте.

---

## 1. Как пользоваться каталогом сейчас

### 1.1. Экран каталога

Вкладка **Sources** получила карточку каталога (`proxy_workbench/ui/index.html:1419-1499`, JS — `renderCatalog`, `catalogRowHtml`, `loadCatalogDetail`, `previewSource`, `catalogAction`, `reloadCatalog`, добавлены в `proxy_workbench/ui/app.js` в рабочем дереве). Внутри неё:

- строка ревизии (`catalog-revision`) и кнопка **Update catalog** (`catalog-refresh`);
- карточки наборов (`catalog-sets`) и групп условий доступа (`catalog-groups`);
- поиск и шесть фильтров: `catalog-q` (имя, ID, издатель, хост), состояние, категория, протокол, формат данных, условия доступа, набор;
- список источников с постраничной подгрузкой (`catalog-more`), панель деталей (`catalog-detail`), форма «свой список» (`catalog-add-*`) с предпросмотром.

Это **расширение** существующего экрана Sources, а не отдельная страница: `id="catalog"` находится внутри `id="page-sources"` (`index.html:1332` и `index.html:1420`).

### 1.2. Что изменилось для пользователя по сравнению с прежним поведением

1. **По умолчанию выбирается то же самое, что и раньше.** На чистом профиле `sources list` показывает `selected: 55` — это ровно те 55 плоских записей, что были в `sources.json` до работы, перенесённые через `legacy_specs` и `legacy_aliases()` (`proxy_workbench/source_catalog.py:585-596`). Проверено: `.venv/bin/python -m proxy_workbench --data <tmp> sources list --source-state selected --source-limit 500` → 55 строк.
2. **Обновление каталога больше ничего не добавляет в выбор.** `update_sources()` при диктовом каталоге возвращает `new_unselected` и `added=[]` (`proxy_workbench/gui.py:381-393`); новые ID не попадают в набор без подтверждения.
3. **Три разных действия вместо одного.** «Приостановить загрузку» (ID остаётся в наборе, `download_disabled_ids`), «Убрать из набора» (кэш и история сохраняются) и «Исключить уже полученные адреса из текущей области» (данные сохраняются, отменяется) разведены по разным endpoint'ам и по разным DB-эффектам (`proxy_workbench/gui.py:1261-1284`; тесты `test_source_management.py:323`, `:526`, `:559`).
4. **Свой URL требует явного выбора формата** (`--source-format` / select `catalog-add-kind`), а не молчаливого «это HTTP-список». Формат, который вы набрали, сохраняется при миграции (`test_source_management.py:206`, `:230`).
5. **Preview ничего не пишет в базу.** `collect(..., preview=True)` не трогает `candidates` и `source_observation` (`test_source_system.py:212-221`); бюджет байтов у preview теперь равен бюджету сборщика, а тело сверх него читается как префикс и так и помечается (`test_source_system.py:489`, `:534`; правки в `CHANGELOG.md`).
6. **В таблице источников появились наблюдаемые состояния вместо «Working».** Новый список показывает HTTP-состояние, состояние формата, возраст кэша/last-good, счётчики распознано/принято/отклонено, эксклюзивный вклад в этом срезе. **Оговорка:** старая таблица «Source report» с колонкой `Working` осталась на той же вкладке и **не переименована** (`index.html:1548`, `col.working` в `app.js:246`) — пункт плана «переименовать колонку Working» не выполнен.
7. **Свой выбор переживает обновление каталога.** Набор — материализованный снимок ID (`apply_set`, `proxy_workbench/source_catalog.py:953-982`; тест `test_source_system.py:101`), `auto_add_new=false` у всех наборов. Отключения хранятся отдельно от membership и не сбрасываются.

### 1.3. Наборы

Восемь наборов (`proxy_workbench/sources.json`, секция `sets`; константы — `source_catalog.py:53-91`):

| id | Имя | Тип | Записей | Из них в наборе на чистом профиле |
|---|---|---|---|---|
| `quick` | Базовый быстрый | system | 8 | 5 |
| `extended` | Расширенный | system | 0 | 0 |
| `experimental` | Экспериментальные | system | 0 | 0 |
| `protocol:http` | HTTP | protocol | 99 | 35 |
| `protocol:https` | HTTPS | protocol | 60 | 8 |
| `protocol:socks4` | SOCKS4 | protocol | 53 | 6 |
| `protocol:socks5` | SOCKS5 | protocol | 64 | 7 |
| `custom` | Пользовательские | local | 0 | 0 |

Источник (команда `.venv/bin/python -m proxy_workbench --data <tmp> sources sets`, фактический вывод):

```text
quick              Базовый быстрый                    system    записей: 8, 5 in set
extended           Расширенный                        system    записей: 0
experimental       Экспериментальные                  system    записей: 0
protocol:http      HTTP                               protocol  записей: 99, 35 in set
protocol:https     HTTPS                              protocol  записей: 60, 8 in set
protocol:socks4    SOCKS4                             protocol  записей: 53, 6 in set
protocol:socks5    SOCKS5                             protocol  записей: 64, 7 in set
custom             Пользовательские                   local     записей: 0
```

Набор `quick` — восемь ID: `cur-02, cur-04, cur-46, cur-51, cur-55, new-024, new-009, new-045` (`source_catalog.py:50-51`). Все восемь коллектабельны; пять из них уже входят в унаследованные 55, три (`new-024`, `new-009`, `new-045`) — нет, поэтому показываются как «новые члены» и в выбор не добавляются. Это рекомендация исследования, а не измерение: **ни один прокси из них не проверялся**.

Набор `experimental` пуст — приложение не придумывает его содержимое.

### 1.4. Свой URL и preview

CLI: `proxy-workbench sources add <url> --source-format line|json-records|fields|page-json|html-table` (`SOURCE_SUBCOMMANDS`, `proxy_workbench/proxytool.py:3174-3176`; тесты `test_source_management.py:449`, `:674`). GUI: форма `catalog-add-*` с кнопками Preview и Add.

Preview отвечает тем же `collect(..., preview=True)` с теми же лимитами, тем же адаптером и той же проверкой URL, показывает распознанное/отклонённое/причины и **не добавляет ни одного кандидата** (`test_source_management.py:479`, `test_source_system.py:212`, `test_source_management.py:716`).

### 1.5. Обновление каталога

Кнопка **Update catalog** → `POST /api/sources/refresh` → фоновая задача, прогресс через `GET /api/sources/update-status` (`proxy_workbench/gui.py:1265`, `:1203`). Отчёт содержит `added`, `changed`, `retired`, `revision`, и **ничего не выбирает** (`test_source_management.py:580`). При сетевой/валидаторной ошибке локальный каталог не меняется (`test_source_management.py:609`; в обоих тестах `fetch_catalog` подменён — `tests/test_source_management.py:600` и `:610`).

Принятый удалённый каталог сохраняется в `data/source-catalog.json`; без него используется встроенный `proxy_workbench/sources.json` (`source_catalog.py:521-546`, `api.py:60-68`, `gui.py:420-437`).

CLI-эквивалент — полный набор команд (`proxy_workbench/proxytool.py:3174`):

```text
proxy-workbench sources list|show|sets|set|enable|disable|add|remove|check|update|recover|status|exclude-scope
```

Общие флаги фильтрации: `--source-query`, `--source-set`, `--source-category`, `--source-protocol`, `--source-format-filter`, `--source-access`, `--source-state`, `--source-limit`, `--source-offset` (видны в `sources --help`).

### 1.6. Если после обновления пропала часть источников

Это предусмотренный сценарий, а не поломка:

- Источник, который принятый каталог убрал, **остаётся строкой** в каталоге, продолжает числиться выбранным и его можно снять с набора — тест `test_a_source_retired_by_a_catalog_update_stays_visible_and_removable` (`tests/test_source_management.py:183`).
- Причина видна в строке (состояние `retired`), а в деталях — в разделе исследовательских полей (`source_management._research`, `source_management.py:701`).
- Снять его: `proxy-workbench sources remove <id>` или кнопка удаления из набора на экране. Кэш и история наблюдений при этом сохраняются (дизайн-план, таблица «Три разных действия»).
- Каталог **нельзя откатить** к более низкой `revision`: `accept_catalog()` отклоняет downgrade, а при одинаковой ревизии с другим содержимым — вообще (`source_catalog.py:564-575`; тест `test_source_system.py:55`).

---

## 2. Сколько чего в каталоге

Источник данных: `proxy_workbench/sources.json` (было 57 строк / 4808 байт, стало 20 717 строк / 1 024 911 байт), корень — `schema_version: 1`, `catalog_id: proxy-workbench.sources`, `revision: 2026092501`, `published_at: 2026-09-25T14:01:37Z`, `minimum_app_version: 2.3.0`.

Подсчёт сделан скриптом через сам загрузчик `source_catalog.load_bundled()` (не разбором JSON вслепую), чтобы считались нормализованные значения.

| Величина | Значение | Как получено |
|---|---|---|
| Всего записей в каталоге | **150** | `len(cat['sources'])` |
| Наборов (`sets`) | **8** | `len(cat['sets'])` |
| Доступно текущему сборщику (`collectable_source`) | **109** | `sum(collectable_source(s))` |
| Не коллектабельно | **41** | 150 − 109 |
| …из них без зарегистрированного адаптера (`adapter.kind is None`) | **34** | перебор `adapter.kind` |
| …из них помечены `adapter.kind == "unsupported"` | **7** (`new-011, new-012, new-050, new-052, new-053, new-057, new-065`) | тот же перебор |
| …из них дополнительно имеют `collection_allowed: false` | **22** | пересечение флагов; отдельно флагом `collection_allowed` исключён **0** записей — он всегда идёт вместе с другой причиной |
| Экспериментальных (набор `experimental`) | **0** | `len(sets['experimental']['members'])` |
| Требуют аккаунта или ключа (`account_required: true`) | **15** | перебор `access.account_required` |
| Включены по умолчанию (`enabled_by_default: true`) | **54** | перебор |
| Источников без сохранённого образца исследования | **3** (`new-062, new-082, new-083`) | сверка с `proxy-sources-research-2026-09-25/samples/*.bin` |

Распределение по подключённому сборщику (`adapter.kind`):

```text
line           77
json-records   23
html-table      5
page-json       3
fields          1
(нет адаптера) 34
unsupported     7
```

Распределение по условиям доступа (`access.kind`):

```text
fully_public_free     117
unknown                11
own_infrastructure     10
paid                    4
temporary_trial         3
permanent_free_quota    2
snapshot_unavailable    2
free_with_api_key       1
```

Причины, по которым запись не является списком адресов (`source_management.not_proxy_reason`, `source_management.py:83-104`): `no_adapter` 14, `commercial_page` 8, `snapshot_unavailable` 7, `self_hosted` 6. Через CLI-фильтр это состояние `not_proxy_source` — 35 строк.

Состояния через CLI (`--source-state <X> --source-limit 500`, фактический подсчёт строк):

```text
selected: 55   unselected: 95   disabled: 0     not_proxy_source: 35
needs_access: 22   rights_unresolved: 0   never_checked: 150
has_data: 0    last_good: 0      stale: 0       failed: 0
quarantined: 0   collectable: 109   retired: 0   custom: 0
```

---

## 3. Какие адаптеры подключены

Реестр — `proxy_workbench/source_adapters.py:18` и `:835-841`; все пять работают как чистые функции над байтами ответа, без `eval`/`exec`/импорта из ответа и без исполнения JS.

| kind | Модуль | Что доказывает конкретный тест |
|---|---|---|
| `line` (77 источников, 6 профилей: `http-v1`, `text-v1`, `socks4-v1`, `socks5-v1`, `auto-v1`, `http-fields-v1`) | `source_adapters._line` | `tests/test_research_collect_integration.py:100` — девять legacy-kind на настоящих сохранённых образцах исследования, у каждого `complete` и `accepted > 0`; `tests/test_source_system.py:735` — битая строка отклоняется, не убивая источник. Ранее: `tests/test_parsing.py`, `tests/test_workbench.py` |
| `json-records` (23 источника, 18 профилей) | `source_adapters._json_records` | `tests/test_source_system.py:163` (`new-009`), `tests/test_research_collect_integration.py:72`; `tests/test_source_system.py:225-243` — пустое тело, HTML-заглушка, невалидный JSON и credentials дают четыре разных наблюдаемых исхода |
| `fields` (1 источник, `generic-fields-v1`) | `source_adapters._fields` | `tests/test_source_system.py:165-181` на сохранённом образце `new-026`; `tests/test_research_collect_integration.py:75,83` |
| `page-json` (3 источника: `page-number-v1` ×2, `geonode-v1` ×1) | `source_adapters._json_records` + `page_info` + `next_page_url` | `tests/test_source_system.py:184` — `new-045` обходит две страницы и останавливается на объявленном `total` (`pages == 2`, `unique == 162`); `tests/test_research_collect_integration.py:76,84` |
| `html-table` (5 источников, `generic-html-v1`) | `source_adapters._html_table` | `tests/test_source_system.py:683-733` пять тестов: объявленные колонки читают не-первую таблицу; layout-таблица перед целевой не тратит бюджет вложенности; бюджет всё равно защищает саму таблицу; вложенная таблица не сдвигает ordinal; JSON-конверт с HTML-фрагментом читается только как разметка. `tests/test_research_collect_integration.py:77,85` |
| `unsupported` (7 источников) | явная отметка, парсер отсутствует | `tests/test_research_collect_integration.py:179` — каждая из шести (`new-011, new-012, new-050, new-052, new-057, new-065`) помечена `unsupported`, не коллектабельна и отдаёт причину `no_adapter`; `new-053` отмечен как `snapshot_unavailable` (исследование зафиксировало HTTP 404) |

Пять доказательных статусов (`source_catalog.EVIDENCE_STATES`, `source_catalog.py:43-49`) хранятся раздельно и по факту заполнены так:

```text
found_in_docs          supported            150
url_reachable          http_2xx_nonempty    141
url_reachable          http_error             6
url_reachable          not_run                3
format_confirmed       not_run              150
data_looks_refreshable validator_present    123
data_looks_refreshable unknown               27
proxy_liveness         not_run              150
```

`proxy_liveness` не выставлен ни у одной записи — и не может быть: тест `test_five_evidence_statuses_stay_separate_and_liveness_is_never_set` (`tests/test_source_management.py:56`).

---

## 4. Аккаунт/ключ и неподдерживаемые записи

### 4.1. Требуют аккаунта или ключа — 15 записей

`account_required: true` (`new-001, new-002, new-003, new-004, new-005, new-006, new-007, new-015, new-047, new-068, new-069, new-070, new-071, new-072, new-082`). Все 15 **не коллектабельны**: у них нет адаптера, а `payload_role` не `proxy_list`.

- **10 — провайдерские** (`paid` 4, `temporary_trial` 3, `permanent_free_quota` 2, `free_with_api_key` 1): Webshare, Bright Data, Decodo, IPRoyal, SOAX, Oxylabs, ProxyRack, Proxy-Daily, Proxy11 API.
- **5 — собственная инфраструктура**, помеченные как требующие учётной записи: 3proxy, Tinyproxy, Squid, Xray-core, WireGuard на своём VPS.

Что сделано для них: они **остаются видимыми в каталоге** с блоком условий доступа, датой проверки первоисточника и ссылкой на Terms/документацию; для них есть отдельные группы в GUI (`ACCESS_GROUPS`, `source_management.py:27-37`) и CLI-фильтр `--source-access`. Они не материализуются в спецификацию загрузки (`_materialization_spec`, `source_catalog.py:683-698`; проверка в `proxytool.py:3389`, `gui.py:579`). Регистрация, активация и оплата не выполнялись, ключи не запрашивались.

Состояние CLI `needs_access` даёт **22** строки — это шире, чем просто «нужен аккаунт»: счётчик `access_blocked_reason` (`source_management.py:105-111`) складывается из 15 записей с `account_required: true`, 4 прочих `own_infrastructure`, 2 `snapshot_unavailable` и 1 `paid` без `account_required` (13 из них, как и 15, не коллектабельны).

Ещё 4 записи `own_infrastructure` (всего 10) аккаунта не требуют — это рецепты self-hosted; причина та же: `self_hosted`.

### 4.2. Неподдерживаемые — 41 запись

Причины (перебор по `collectable_source`, `source_catalog.py:629-641`):

| Причина | Записей | Что это |
|---|---|---|
| `payload_role: unknown` + адаптера нет + `collection_allowed: false` | 21 | коммерческие страницы, дашборды, конфиги клиентов, не-списки |
| `payload_role: unknown` + адаптера нет | 13 | в том числе 7 «снимок недоступен» и 6 self-hosted |
| `adapter.kind: "unsupported"` | 6 | адрес или порт пишется через `document.write()`, либо страница рендерится на клиенте |
| `adapter.kind: "unsupported"` + `collection_allowed: false` | 1 | `new-053`, исследование зафиксировало HTTP 404 |

Семь помеченных как неподдерживаемые форматом — `new-011, new-012, new-050, new-052, new-053, new-057, new-065`. Каждая показывает честную причину вместо обычной строки списка; это и проверяется тестом `test_sources_the_app_cannot_read_say_why_instead_of_looking_ordinary` (`tests/test_research_collect_integration.py:179`).

### 4.3. Права на данные

У **всех 150** записей `rights_status: unknown` и `rights_approved: false`; `rights.code_license`/`data_license` не установлены (`rights_approved` проставляется вручную и нигде автоматически не становится `true`).

Практическое следствие, которое стоит знать: функция-гейт прав существует — `eligible_sources(catalog, require_rights=True)` (`source_catalog.py:661-670`) — но **она нигде не вызывается**: поиск по всему репозиторию даёт только её определение. Состояние `rights_unresolved` в CLI даёт 0 строк. То есть запрет «скачивать без прав» формально описан, но не enforced. Это пункт плана, не выполненный.

---

## 5. Фактические результаты выполненных проверок

Все команды запускались из корня `/Users/main/Desktop/111/proxy-workbench-sources`.

### 5.1. Полный набор тестов — **пройден**

```text
.venv/bin/python -m unittest discover -s tests
...
----------------------------------------------------------------------
Ran 203 tests in 71.605s

OK
```

(В выводе есть диагностические строки `Executing <Task ...> took ...` от asyncio и `Source N: rows ...` от тестов с локальными mock-сервисами — это отладочный вывод существующих тестов, а не падения.)

### 5.2. Baseline 135 — **не ухудшился**

```text
.venv/bin/python -m unittest tests.test_anonymity tests.test_api tests.test_bandwidth \
  tests.test_extras tests.test_freshness tests.test_gateway tests.test_geo_want tests.test_gui \
  tests.test_i18n tests.test_parsing tests.test_paths tests.test_prefilter tests.test_providers \
  tests.test_regressions tests.test_selection tests.test_socks4 tests.test_sources_gui tests.test_workbench
...
Ran 135 tests in 41.484s

OK
```

18 существовавших модулей — ровно те 135 тестов из постановки. Прирост: 203 − 135 = **68 новых тестов** в трёх новых файлах.

### 5.3. Smoke — **пройден**

```text
.venv/bin/python packaging/smoke.py .venv/bin/proxy-workbench
Proxy Workbench 2.2.1
{"exit_code": 0, "passed": 1}
smoke test passed
```

### 5.4. Новые тестовые модули — по отдельности, все `OK`

```text
.venv/bin/python -m unittest tests.test_source_system
Ran 29 tests in 4.206s
OK

.venv/bin/python -m unittest tests.test_source_management
Ran 34 tests in 15.112s
OK

.venv/bin/python -m unittest tests.test_research_collect_integration -v
Ran 5 tests in 9.002s
OK
```

29 + 34 + 5 = 68 — сходится с приростом.

### 5.5. Падений нет

Ни один из запусков не завершился ошибкой, пропуском или падением. `test_research_collect_integration` **не был пропущен**: он читает сохранённые образцы из `../proxy-sources-research-2026-09-25/samples` (153 файла `.bin`), 147 из 150 каталоговых источников имеют свой образец; при отсутствии каталога он был бы `SkipTest`, но в этом заходе запустился и прошёл (`setUpClass` → `REQUIRED_SAMPLES` найдены).

### 5.6. Фактический diff относительно `806d13d`

```text
$ git diff --stat 806d13d
 CHANGELOG.md                  |   29 +
 proxy_workbench/api.py        |   91 +
 proxy_workbench/branding.py   |    6 +-
 proxy_workbench/gui.py        |  436 +-
 proxy_workbench/proxytool.py  | 1903 +++-
 proxy_workbench/sources.json  | 20774 +++++++++++++++++++++++++++++++++++++++-
 proxy_workbench/ui/app.js     |  682 ++
 proxy_workbench/ui/index.html |   92 +
 proxy_workbench/ui/style.css  |  311 +
 9 files changed, 24211 insertions(+), 113 deletions(-)
```

Новые (untracked) файлы — `git ls-files --others --exclude-standard`:

```text
docs/source-system-design.ru.md            73 806 байт (дизайн-план, не отчёт)
proxy_workbench/source_adapters.py          942 строки
proxy_workbench/source_catalog.py          1046 строк
proxy_workbench/source_management.py        894 строки
tests/test_research_collect_integration.py  219 строк
tests/test_source_management.py             822 строки
tests/test_source_system.py                 752 строки
ultra-sources.dwf.ts                        файл динамического воркфлоу
```

`proxy_workbench/sources.json`: 57 строк / 4808 байт → 20 717 строк / 1 024 911 байт.

### 5.7. Что запускалось вне автоматических проверок

Команда `.venv/bin/python -m proxy_workbench --data <временная папка> sources …` — реальный CLI на временных данных, без сети: `sources list`, `sources sets`, `sources list --source-state …`, `sources list --source-access …`, `sources --help`. Выходные строки процитированы в разделах 1.2 и 2.

---

## 6. Оставшиеся внешние блокеры и сознательно не сделанное

### 6.1. Блокеры вне моего досягаемости

1. **Публичного каталога не существует.** `SOURCE_CATALOG_URL` указывает на `https://raw.githubusercontent.com/DavidVoitenko/proxy-workbench/main/proxy_workbench/sources.json` (`branding.py:17-20`), а в `main` по-прежнему лежит старый плоский список. **Ничего не публиковалось и не коммитировалось** (по условиям задачи), поэтому `sources update` против настоящего удалённого URL я **не проверял** — в тестах `fetch_catalog` подменён на mock (`tests/test_source_management.py:600` и `:610`). Вся приёмка обновления каталога проходит на подставном каталоге.
2. **Версия приложения 2.2.1, а `minimum_app_version` каталога — 2.3.0** (`pyproject.toml:7`, `branding.py:14`, корень `sources.json`). Поле `minimum_app_version` только переносится валидатором и дальше нигде не проверяется (поиск по `proxy_workbench/*.py`: только `source_catalog.py:108,511` и `source_management.py:568`). То есть каталог, требующий более новой версии, будет принят без предупреждения. Пунктом плана это не было, но при публикации — риск.
3. **Права на данные не установлены ни для одной записи.** Без решения владельца проекта `rights_approved` останется `false` у всех, и гейт прав не заработает.
4. **Ни один прокси не проверялся** — ни здесь, ни в исследовании. `format_confirmed: not_run` у всех 150 записей. Статус «прокси проверены» в этом проекте не выставляется вообще.

### 6.2. Реализовано в коде, но НЕ доказано тестом

Проверено поиском по `tests/`: эти коды и ветки существуют в коде, но ни один тест их не утверждает.

| Механизм | Где в коде | Покрыт тестом? |
|---|---|---|
| `Retry-After` (парсинг и ожидание) | `proxytool.py:1219`, `:1535-1536`, `:1660` | **Нет** — `Retry-After`/`retry_after`/`SOURCE_RATE_LIMITED` не встречаются в тестах |
| Backoff и карантин по времени | `proxytool.py:1579-1595` | Косвенно: `test_source_management.py:559` заносит строки в БД вручную и проверяет `recover` |
| Вытеснение кэша `cache_evicted` | `proxytool.py:1393` | **Нет** |
| Режимы пагинации `offset` / `cursor` / `next-url` | `source_adapters.next_page_url`, `source_adapters.py:903` | **Нет** — покрыт только `page-number` |
| Блокировка внешнего `next` хоста (`SOURCE_PAGINATION_NEXT_INVALID`) | `source_adapters.py:934, 938` | **Нет** |
| Дрейф заголовка HTML-таблицы (`SOURCE_HTML_SCHEMA_CHANGED`) | `source_adapters.py:634, 639` | **Нет** |
| Ошибка CSV (`SOURCE_CSV_INVALID`) | `source_adapters.py:428` | **Нет** |
| Monosans: `host != exit_ip` не смешиваются | `_metadata`, `source_adapters.py:187` | **Нет** |
| `rights_approved` / `require_rights` | `source_catalog.py:668` | **Нет** — функция вообще не вызывается |
| `minimum_app_version` | `source_catalog.py:511` | **Нет** — только переносится |

### 6.3. Сознательно не сделано

1. **Регистрация, ключи, платные и trial-фиды, приватные фиды** — вне scope и по условиям задачи. Все 15 таких записей остаются видимыми, но недоступными сборщику.
2. **Self-hosted 3proxy/Squid/Xray/WireGuard** — 10 записей, видимы, не собираются.
3. **Base64-подписки, произвольный YAML/JS, браузерная автоматизация, MTProto, исполнение конфигураций** — не реализовано. Адреса, которые пишутся через `document.write()`, помечены неподдерживаемыми, а не «прочитаны».
4. **Массовая проверка liveness прокси из исследования** — не выполнялась.
5. **Автоматическое включение 150/55/all** — не делается; наборы материализованы, `auto_add_new=false` у всех.
6. **Юридическое заключение и вывод data-лицензии из лицензии кода** — не делалось; у всех 150 `data_license: unknown`.
7. **Новый gateway, новая модель аутентификации API, новый формат снапшота, второй backend** — не вводились. Единственное расширение — три leaf-модуля: `source_catalog`, `source_adapters`, `source_management`.
8. **Автоудаление старых кандидатов при disable/remove** — не делается; для этого существует явная отменяемая scope-exclusion.
9. **Живая перепроверка всех 150 URL** — не выполнялась. Команда `sources check` проверяет только явно указанный источник и только доступность/формат.
10. **Переименование колонки `Working`** в таблице «Source report» — пункт плана, не выполненный (`index.html:1548`). Новая карточка каталога рядом с ней построена на наблюдаемых состояниях, но старая таблица осталась.

---

## 7. Подтверждённое и непроверенное

### Подтверждено выполненными в этой сессии проверками

- Полный suite: **203 теста, OK**; baseline-модули: **135 тестов, OK**; smoke: **passed**. Всё запускалось здесь, 2026-09-25.
- Каталог — валидный по собственному валидатору: 150 записей, 8 наборов, `revision 2026092501`, `schema_version 1`.
- 109 из 150 записей коллектабельны текущим сборщиком; у 108 из них есть сохранённый образец исследования, и адаптер её же записи этот образец реально разбирает и даёт адреса. Единственная коллектабельная запись без образца — `new-062` (Docip free JSON feed), образцов для неё в `samples/` нет, поэтому её адаптер ничем не проверен. Два исключения среди проверенных — `cur-37` и `new-086` — исследование само зафиксировало как пустые на момент проверки (`tests/test_research_collect_integration.py:177`).
- Все пять доказательных статусов хранятся раздельно; `proxy_liveness = not_run` у всех 150 и не выставляется тестом.
- Preview не пишет ни кандидатов, ни наблюдений.
- Обновление каталога (на подставном удалённом каталоге) не выбирает ничего; при ошибке локальный каталог не меняется.
- Пять legacy-kind (`http`, `https`, `socks4`, `socks5`, `socks5h`, `auto`, `text`, `http-fields`, `geonode`) по-прежнему читают настоящие сохранённые образцы без регрессий.

### Не проверено

- **Ни один прокси не проверялся.** Ни один адрес ни в одном источнике не был подключён. В каталоге нет и не будет статуса «рабочие» / «качественные» / «проверенные по прокси».
- `format_confirmed` у всех 150 = `not_run`: формат подтверждён **тестом адаптера на сохранённом образце**, а не обращением к живому источнику.
- `url_reachable` — это снимок на дату `checked_at` из исследования (141 `http_2xx_nonempty`, 6 `http_error`, 3 `not_run`). Сегодняшняя доступность URL не проверялась.
- Удалённый каталог по `SOURCE_CATALOG_URL` не запрашивался (нечего запрашивать — он не опубликован, и публиковать запрещено).
- `docs/source-system-design.ru.md` в разделе 2.3 фиксирует baseline 137 и две async-проверки в `test_research_collect_integration.py`; к моменту этого отчёта в файле **5** тестов, а baseline без него — ровно 135, как в постановке. Расхождение объясняется, но цифра «137» в дизайн-доке устарела.
- Всё, что перечислено в разделе 6.2, существует в коде по прочтению, но не имеет теста.

### Правила каталога, которые в отчётности соблюдены

Ни один источник не назван рабочим, качественным или проверенным по прокси. Совпадение наборов и уникальность адресов в этом документе нигде не трактуются как доказательство происхождения, владения или качества. HTTP 200 с пустым телом везде фигурирует как «пусто на момент проверки», отдельным состоянием `empty_body` (`source_management.FETCH_STATES`, `source_management.py:59-60`). Бесплатный tier ни одного провайдера не выдан за единственный на рынке. Лицензия кода репозитория нигде не приравнена к лицензии на данные — у всех 150 `data_license: unknown`. Условия доступа снабжены ссылкой на первоисточник и датой проверки; ключевые цитаты по провайдерам взяты из записи `access.quota_text` в самом каталоге.
