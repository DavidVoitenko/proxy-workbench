# Модель каталога источников, адаптеры и устойчивый сбор

**Статус:** проект для следующей итерации, не выполненная реализация  
**Дата проектирования:** 2026-09-25  
**Область:** изолированная копия `sources-catalog`

## 1. Решение в одном абзаце

Каталог остаётся данными, а не программируемым расширением: публичная версия лежит в пакете, имеет `schema_version` и монотонную `revision`, а каждый источник имеет неизменяемый `id`, не вычисленный из URL. `proxytool.collect()` остаётся единственным сетевым сборщиком; новые модули могут содержать только проверку каталога и чистые функции разбора. Текущий `gui-settings.json` мигрируется в версию 3 с материализованным списком выбранных ID, поэтому удалённый ранее встроенный источник не возвращается, новый источник не включается сам, а пользовательский URL сохраняется как отдельная запись. В SQLite добавляются наблюдения источников, поколения last-good, many-to-many provenance и метаданные, заявленные источником; результаты приложения остаются в существующей таблице `results`. `304`, пустой ответ, HTML-заглушка, невалидный JSON, `429`, таймаут и превышение лимита получают разные наблюдаемые состояния и не удаляют последний полный набор.

## 2. Что реально установлено в этой копии

### 2.1. Текущий источник истины

1. `proxy_workbench/sources.json:1-57` — плоский JSON-массив из 55 строковых URL/вида `kind URL`. Это не каталог с метаданными: у записи нет ID издателя, формата, прав, даты проверки или состояния.
2. `proxy_workbench/proxytool.py:476-499` — девять текущих видов: `http`, `https`, `socks4`, `socks5`, `socks5h`, `auto`, `text`, `geonode`, `http-fields`; URL проверяется до запроса.
3. `proxy_workbench/proxytool.py:502-771` — один `collect()` читает локальные файлы и удалённые источники, выполняет до двух попыток, ограничивает общий параллелизм восемью задачами и пишет отчёт. `geonode` — единственный текущий структурированный JSON-путь.
4. `proxy_workbench/proxytool.py:433-452` — `open_db()` включает WAL и создаёт `candidates`, `candidate_meta`, `candidate_seen`, `profiles`, `results`. У `candidate_seen` уже есть many-to-many, но нет времени первого/последнего наблюдения; `candidate_meta.source` хранит только первый источник.
5. `proxy_workbench/proxytool.py:309-327` — финальный `normalize()` требует глобальный IP, порт и поддерживаемую схему, отбрасывает credentials, query, fragment, путь, private/reserved IP и SOCKS4-over-IPv6. Этот барьер должен остаться последним барьером любого адаптера.
6. `proxy_workbench/proxytool.py:112-232` и `proxy_workbench/proxytool.py:654-698` — действуют проверка HTTP(S) URL, блокировка private/metadata destination, DNS pinning, запрет downgrade и лимит редиректов. Эти проверки нельзя обходить новым профилем адаптера.
7. `proxy_workbench/proxytool.py:44-53`, `proxy_workbench/proxytool.py:255-306` и `proxy_workbench/proxytool.py:2036-2053` — существующие границы: 32 MiB на источник, 64 KiB на строку, 500 тыс. кандидатов, пять редиректов, плюс абсолютные максимумы.
8. `proxy_workbench/gui.py:79-179` — `gui-settings.json` имеет версию 2, а `sources` является плоским списком выбранных URL; текущая валидация принимает только строки старого формата.
9. `proxy_workbench/gui.py:268-301` — существующие `/api/sources/prune` и `/api/sources/update` уже являются точками изменения выбора. `prune` сейчас опирается на отсутствие прошедших прокси, а `update` молча дописывает новые URL; обе семантики нужно заменить, а не добавить рядом вторым путём.
10. `proxy_workbench/api.py:0-5`, `proxy_workbench/api.py:80-152` и `proxy_workbench/api.py:206-302` — публичный локальный API уже реализует read-only модель: coherent export generation, `GET`/`HEAD`, bearer token для не-loopback и trusted-host проверка.
11. `proxy_workbench/ui/index.html:1331-1474` и `proxy_workbench/ui/app.js:2930-2983` — экран Sources уже показывает отчёт, но это текстовое поле URL и колонка «Working», то есть нет каталога, preview и доказательных состояний.
12. `proxy_workbench/branding.py:16-17` публикует `SOURCES_URL` на `sources.json` в `main`; bundled-файл при этом находится по пути `proxy_workbench/sources.json` и включён в package-data и PyInstaller (`pyproject.toml:41-45`, `packaging/proxy-workbench.spec:7-12`).

### 2.2. Что подтверждено исследованием

1. Исследование разобрало 150 источников, но не проверило ни один прокси: 140 URL дали непустой HTTP-ответ, 121 имел ETag/Last-Modified, а `proxies_actually_tested=false` у всех (`../proxy-sources-research-2026-09-25/01_SUMMARY.ru.md:10-30`).
2. Пять статусов должны оставаться раздельными: найден по документации; URL ответил; формат подтверждён; данные выглядят обновляемыми; прокси проверены. Последний в проекте не выставляется (`../proxy-sources-research-2026-09-25/01_SUMMARY.ru.md:16-30`).
3. HTTP 200 с пустым телом — отдельное состояние «пусто на момент проверки», а не доказательство постоянной недоступности (`../proxy-sources-research-2026-09-25/01_SUMMARY.ru.md:41-55`).
4. Совпадение наборов доказывает только совпадение в конкретном срезе; из него нельзя выводить общего владельца, копирование или происхождение (`../proxy-sources-research-2026-09-25/01_SUMMARY.ru.md:125-140`).
5. Уникальность адресов источника в срезе не означает работоспособность или качество (`../proxy-sources-research-2026-09-25/01_SUMMARY.ru.md:138-146`).
6. Лицензия кода репозитория не является лицензией на опубликованные данные и не покрывает чужие адреса без отдельного основания (`../proxy-sources-research-2026-09-25/00_INDEX.ru.md:60-68`).
7. Исследовательский план рекомендует будущий базовый набор из восьми источников, но отдельно требует fixture, проверку формата, rights gate и не считает это доказательством liveness (`../proxy-sources-research-2026-09-25/notes/integration-plan.ru.md:164-179`).

### 2.3. Локально выполненные проверки

Полная проверка запускалась из корня изолированной копии, без публичной сети и без соединений через найденные прокси.

```text
.venv/bin/python -m unittest discover -s tests
Ran 137 tests in 41.181s

OK
```

Фактическое число в этой копии — **137**, а не 135 из постановки. До начала проектирования `git status --short` уже показывал untracked `tests/test_research_collect_integration.py`; в нём два async-теста (`tests/test_research_collect_integration.py:20-149`). Тесты вывели несколько `ResourceWarning` и slow-asyncio предупреждений, но завершились `OK`.

```text
.venv/bin/python packaging/smoke.py .venv/bin/proxy-workbench
Proxy Workbench 2.2.1
{"exit_code": 0, "passed": 1}
smoke test passed
```

Smoke использует локальный `ThreadingHTTPServer`, временную data-папку и локальный mock proxy (`packaging/smoke.py:25-37`, `packaging/smoke.py:72-115`). Проверка пройдена. Это не проверка источников из каталога и не proxy-liveness проверка.

## 3. Неподвижные принципы

1. **Один сборщик.** Сетевой код, redirect policy, SSRF/DNS pinning, budgets, retry и запись в кандидатов остаются в `proxytool.collect()` и его локальных помощниках. Второго `download_sources()`, второго worker protocol или второй БД не будет.
2. **Данные, не код.** Каталог не содержит import path, модулей, shell/JS, шаблонов исполнения или callback. Профиль адаптера выбирает одну из зарегистрированных в приложении реализаций.
3. **Последняя нормализация общая.** Адаптер может распознать поля, но кандидат добавляется только через существующий `normalize()` и текущий denylist.
4. **Пользовательский выбор — снимок.** Набор при применении материализуется в ID. Новая запись каталога не присоединяется к уже применённому набору автоматически.
5. **Историческое доказательство не обновляется текущим статусом.** Catalog evidence с датой исследования и runtime observation — разные поля.
6. **Liveness источника не существует.** Приложение может показать, сколько адресов источника прошли конкретный профиль, но не называть источник «рабочим» или «качественным».
7. **304 — не новый контент.** Он не меняет candidate set, generation, first/last-seen и не продлевает `valid_until` результатов прокси.
8. **Failure не удаляет данные.** Ошибка сети, парсинга, лимита или отмена сохраняют последнюю полную generation и показывают её возраст/причину отказа.
9. **Никаких новых обязательных полей в snapshot v1.** `SNAPSHOT_SCHEMA_VERSION=1`, `export_manifest` и consumer-контракт остаются как есть (`proxy_workbench/proxytool.py:363-385`, `proxy_workbench/proxytool.py:1654-1688`).

## 4. Минимальная раскладка модулей

### 4.1. Существующие точки ответственности

| Файл | Что меняется | Чего не будет |
|---|---|---|
| `proxy_workbench/proxytool.py` | `open_db()` добавляет source-схему v1; `collect()` получает планы источников, conditional GET, last-good, budgets и пишет provenance; `source_spec()` сохраняет совместимость со строками; CLI вызывает тот же resolver | Новый сетевой стек, новая БД, отдельный backend service |
| `proxy_workbench/gui.py` | Валидирует settings v3; локальные endpoints для каталога, preview, выбора и recovery; передаёт worker-у материализованный plan | Собственный fetch/parser и отдельный GUI-процесс |
| `proxy_workbench/api.py` | Только read-only `GET /sources`, `GET /sources/{id}` и, при необходимости, `/source-sets`; читает SQLite WAL в короткой read-only transaction | Никаких mutating endpoints или обхода token/host policy |
| `proxy_workbench/ui/*` | Существующий экран Sources превращается в каталог; термины и действия переводятся через существующий i18n | Новый SPA framework и внешняя зависимость |
| `proxy_workbench/maintenance.py` | При необходимости учитывает новые runtime-файлы, если они появятся; settings v3 всегда сохраняется | Не удаляет пользовательский выбор при `clear-data` |
| `proxy_workbench/branding.py` | `SOURCE_CATALOG_URL`; `SOURCES_URL` остаётся временным alias | Не исполняет загруженный каталог |

### 4.2. Новые leaf-модули

Два новых модуля оправданы только как чистые библиотеки, которые уменьшают 2304-строчный `proxytool.py` и не создают параллельную архитектуру:

1. **`proxy_workbench/source_catalog.py`**
   - загрузка и строгая валидация bundled/remote JSON;
   - стабильные ID, legacy aliases, разрешение наборов;
   - миграция старого `sources`;
   - redacted public view;
   - ноль HTTP, SQLite, subprocess и исполнения данных каталога.
2. **`proxy_workbench/source_adapters.py`**
   - реестр чистых адаптеров `line`, `json-records`, `fields`, `page-json`, `html-table`;
   - декларативные profile maps и нормализация заявленных metadata;
   - ноль HTTP, SQLite, DNS, файловой записи и `eval`/`exec`/`import` ответа.

`collect()` импортирует эти функции, выполняет HTTP и является единственной commit-точкой. Если позже понадобится отдельный storage-helper, его нельзя добавлять без повторного измерения связности; в этой итерации он не нужен.

## 5. Публичный каталог

### 5.1. Расположение и версия

Канонический bundled-файл сохраняет путь `proxy_workbench/sources.json`, но меняет корень с массива на объект. Это сохраняет package-data и PyInstaller без нового пути. `branding.py` получает основной `SOURCE_CATALOG_URL` на:

```text
https://raw.githubusercontent.com/DavidVoitenko/proxy-workbench/main/proxy_workbench/sources.json
```

`SOURCES_URL` остаётся alias на время перехода для импортов, но новый код больше не вызывает его как «список URL для дописывания». Так исправляется расхождение между текущим URL без `/proxy_workbench/` и фактическим tracked-файлом пакета.

Корень каталога:

```json
{
  "schema_version": 1,
  "catalog_id": "proxy-workbench.sources",
  "revision": 2026092501,
  "published_at": "2026-09-25T14:01:37Z",
  "minimum_app_version": "2.3.0",
  "sources": [],
  "sets": []
}
```

Правила версии:

1. `schema_version` меняет только структурный контракт; несовместимая версия отклоняется целиком.
2. `revision` — монотонное целое. Более старый remote catalog не заменяет локальный.
3. `published_at` — время публикации данных каталога, не время проверки каждого URL.
4. Bundled catalog всегда доступен офлайн; remote fetch выполняется только по явной команде пользователя.
5. Remote JSON имеет byte limit, проверяется целиком, не исполняется и заменяет bundled metadata только после успешной валидации. Ошибка сохраняет последний принятый каталог.
6. ETag/Last-Modified применяются и к самому catalog fetch, но не к currently exported proxies.
7. Источник не становится выбранным из-за обновления каталога. Новый ID отображается как «доступен в каталоге, не выбран».

### 5.2. Стабильный ID

Публичный `id` — короткая назначаемаяMaintainer-ом строка `[a-z0-9][a-z0-9._-]{2,63}`, например:

- `proxyspace-http`;
- `geonode-page-json`;
- `monosans-json-all`;
- `proxyscrape-v4-socks5`.

ID не вычисляется из URL, позиции, имени или хеша. Он не меняется при смене URL, зеркала или publisher display name. Удалённый ID остаётся tombstone/last-known selection; новый URL никогда не получает ID старого автоматически. Один endpoint, присутствующий у нескольких ID, остаётся many-to-many.

Для custom source используется `custom-<16 hex>` от канонизированного исходного spec. Он сохраняется локально; при изменении пользователем URL создаётся новая запись, а старая не переназначается.

### 5.3. Поля источника

```json
{
  "id": "monosans-json-all",
  "name": "monosans/proxy-list — JSON",
  "publisher": {
    "id": "monosans",
    "name": "monosans",
    "homepage": "https://github.com/monosans/proxy-list"
  },
  "family_id": "monosans-proxy-list",
  "research_refs": ["new-077"],
  "legacy_specs": [],
  "payload_role": "proxy_list",
  "access": {
    "kind": "public",
    "account_required": false,
    "quota_text": "unknown"
  },
  "protocols": ["http", "https", "socks4", "socks5"],
  "adapter": {
    "kind": "json-records",
    "profile": "monosans-v1",
    "config": {}
  },
  "endpoints": [
    {
      "id": "github-pretty",
      "url": "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies_pretty.json",
      "role": "primary",
      "relation": "own_feed"
    }
  ],
  "rights": {
    "terms_url": "https://github.com/monosans/proxy-list/blob/main/LICENSE",
    "code_license": "MIT",
    "data_license": "unknown",
    "checked_at": "2026-09-25T12:00:00Z",
    "attribution": ""
  },
  "evidence": {
    "found_in_docs": {"state": "supported", "checked_at": "2026-09-25T12:00:00Z", "refs": ["new-077"]},
    "url_reachable": {"state": "http_2xx_nonempty", "checked_at": "2026-09-25T12:00:00Z"},
    "format_confirmed": {"state": "confirmed", "checked_at": "2026-09-25T12:00:00Z", "sample_sha256": "..."},
    "data_looks_refreshable": {"state": "validator_present", "checked_at": "2026-09-25T12:00:00Z"},
    "proxy_liveness": {"state": "not_run", "checked_at": null}
  },
  "limits": {},
  "maturity": "stable",
  "catalog_state": "listed",
  "tags": ["metadata", "multi-protocol"]
}
```

Обязательные поля: `id`, `name`, `publisher`, `payload_role`, `access`, `adapter`, `endpoints`, `rights`, `evidence`, `maturity`, `catalog_state`.

Значения `payload_role`: `proxy_list`, `documentation`, `commercial_page`, `self_hosted`, `subscription_config`, `unknown`. В collection gate допускается только `proxy_list` с `access.kind=public` и установленным adapter profile.

### 5.4. Пять доказательных статусов

Catalog evidence неизменяем по времени и не заменяется текущим fetch:

1. `found_in_docs` — `supported|not_found|unknown`;
2. `url_reachable` — только `http_2xx_nonempty`, `http_error` или `not_run`; 2xx не означает format;
3. `format_confirmed` — `confirmed|partial|no_valid_records|unsupported|invalid|not_run`;
4. `data_looks_refreshable` — `validator_present|body_change_observed|unknown`; ETag сам по себе не означает свежие прокси;
5. `proxy_liveness` — в этой системе только `not_run`. Валидатор каталога запрещает `true`/`confirmed` для source-level поля.

Имена вроде `checked.json` остаются provider claim и не превращаются в app evidence.

### 5.5. Runtime fetch states

Evidence и transport state ортогональны. API/GUI возвращают минимум три поля:

- `http_state`: `not_attempted`, `http_2xx_nonempty`, `empty_body`, `not_modified`, `http_429`, `timeout`, `http_error`, `blocked_destination`, `redirect_blocked`, `canceled`;
- `parse_state`: `not_run`, `confirmed`, `partial`, `no_valid_records`, `unsupported`, `invalid`;
- `cache_state`: `none`, `last_good`, `stale_last_good`, `not_modified`, `cache_evicted`.

Производный `outcome` для краткого отображения: `available`, `empty`, `html_placeholder`, `invalid_json`, `rate_limited`, `timeout`, `limit_exceeded`, `partial`, `fallback_used`, `unavailable`. Он не заменяет исходные поля.

## 6. Пользовательский выбор, наборы и opt-in

### 6.1. Модель набора

Набор — **материализованный снимок ID**, а не живой query:

```json
{
  "id": "quick",
  "name": "Базовый быстрый",
  "kind": "system",
  "catalog_revision": 2026092501,
  "members": ["proxyspace-http", "proxyscrape-v4-http"],
  "auto_add_new": false
}
```

Применение набора записывает его ID и concrete member IDs в settings. Позднее обновление каталога не меняет этот список. UI показывает «в наборе появилось N новых источников — не добавлены».

### 6.2. Начальные наборы

| Набор | Назначение | Начальный состав |
|---|---|---|
| `quick` | Рекомендуемый базовый перечень из исследования; не рейтинг liveness | `cur-02`, `cur-04`, `cur-46`, `cur-51`, `cur-55`, `new-024`, `new-009`, `new-045` (`notes/integration-plan.ru.md:164-179`) |
| `extended` | Явно curated, стабильные, public, поддержанные адаптерами и прошедшие rights gate | Задаётся явным `members` в catalog revision; не вычисляется как «все 150» |
| `protocol:http`, `protocol:https`, `protocol:socks4`, `protocol:socks5` | Фильтр/protocol bundle | Применяется только по команде пользователя; сохраняет текущий snapshot IDs |
| `experimental` | Непроверенные форматы/источники с постоянным предупреждением | Только явное добавление; никогда не входит в quick/extended автоматически |
| `custom` | Пользовательские URL и format choice | Только локальные custom definitions; не публикуются в bundled catalog |

`new-024` и `new-045` в исследовании — URL с country/protocol/anonymity filters, поэтому название и описание quick entry должны показывать exact filter, а не обещать все страны или протоколы. Набор `extended` не включает записи с `rights.data_license=restricted/unresolved`, account/key, self-hosted, documentation или experimental без отдельного подтверждения. Состав хранится явно, чтобы новый eligible source не расширял старый набор.

### 6.3. Явный opt-in

1. Первый запуск без существующих данных показывает quick set как предложение и ждёт подтверждения.
2. `extended` и protocol bundles никогда не включают всё автоматически.
3. «Все доступные» — не название набора и не default. Это отдельная двухшаговая команда: preview count → подтверждение concrete ID list.
4. Обновление remote catalog только показывает новые, изменённые и вышедшие из каталога записи.
5. Built-in, удалённый пользователем, остаётся unselected; catalog update не возвращает его.
6. Новый ID никогда не выбирается только из-за `enabled_by_default` или `priority`.

## 7. Миграция без потери выбора

### 7.1. Каталог

Перед переходом each legacy source entry получает:

- новый stable ID;
- `legacy_specs` с точной старой строкой (`kind URL` или URL);
- `research_refs` для прослеживаемости (`cur-*`, `new-*`), но research ID не становится runtime ID.

Это делает mapping детерминированным и позволяет откатить schema parser.

### 7.2. `gui-settings.json`

`settings_version` становится 3. Старое поле `sources` читается один раз и заменяется:

```json
{
  "source_selection": {
    "schema_version": 1,
    "catalog_revision": 2026092501,
    "selected_ids": ["proxyspace-http", "custom-..."],
    "download_disabled_ids": [],
    "sets": [{"id": "legacy-import", "members": ["..."]}],
    "custom_sources": [
      {
        "id": "custom-...",
        "url": "https://user.example/list",
        "adapter": {"kind": "fields", "profile": "user-csv-v1", "config": {}}
      }
    ]
  }
}
```

Алгоритм:

1. Прочитать exact legacy spec из `settings.sources`; не сортировать и не пересобирать по bundled default.
2. Найти exact `legacy_specs` публичного ID.
3. Если exact alias не найден, найти URL, только если он однозначен и catalog entry явно перечисляет этот alias как `legacy_specs`; никогда не угадывать по display name.
4. Иначе создать custom entry с сохранёнными exact URL, kind и adapter selection. Query/secret-like path остаётся локально и redacted в public API/GUI report.
5. Отсутствующие bundled IDs остаются отсутствующими: это и есть прежнее удаление/отключение.
6. Новые IDs из новой revision не добавляются.
7. `use_sources` переносится без изменения. Если он `false`, список всё равно сохраняется для будущего включения.
8. Запись атомарна. Старый `sources` остаётся только в миграционном backup/логе этой операции, но не является вторым authority.

Наличие `proxies.sqlite3` без `gui-settings.json` трактуется как старый пользователь и сначала восстанавливает прежний bundled selection, а не quick set. Только полностью новый data directory получает предложение quick.

### 7.3. SQLite

В `open_db()` появится `SOURCE_DB_SCHEMA_VERSION=1`; он не связан с export `SNAPSHOT_SCHEMA_VERSION=1`.

1. Расширить `candidate_seen` nullable-колонками `first_seen_at`, `last_seen_at`, `first_observation_id`, `last_observation_id`, `legacy`. Существующие строки получают `legacy=1`, времена остаются NULL — дата миграции не выдумывается как дата наблюдения.
2. Старый 16-hex `source_key(spec)` заменяется stable ID через migration map. Сам `source_key()` остаётся для compatibility/reporting и тестов.
3. `candidate_meta.source`, которого нет в `candidate_seen`, переносится как одна legacy membership.
4. Исходная `candidate_meta.country` переносится в `source_metadata` с `origin=source_claimed`, если источник известен; иначе как `legacy_unattributed`. Она не становится app measurement.
5. `results` и app measurements не мигрируются и не смешиваются с source metadata.

Миграция выполняется одной SQLite transaction. На busy/locked DB приложение сообщает ошибку и не создаёт half-migrated settings.

## 8. Контракт адаптеров

### 8.1. Общий pure-контракт

Вход:

```text
parse_page(body: bytes, profile: dict, page_context: dict, limits: Limits) -> ParsePage
```

Выход — обычные dict в стиле проекта:

```json
{
  "state": "complete|partial|empty|invalid|unsupported",
  "records": [
    {
      "value": "socks5://203.0.113.7:1080",
      "protocol_origin": "record|profile|map_key",
      "declared": {
        "country": "DE",
        "asn": 64500,
        "anonymity": "elite",
        "last_checked": 1780000000.0,
        "exit_ip": "198.51.100.9"
      }
    }
  ],
  "rejects": {
    "invalid_address": 4,
    "unsupported_protocol": 1,
    "credentials_present": 1
  },
  "next": null,
  "pages": 1
}
```

Правила:

1. Адаптер не пишет SQLite/cache/settings и не выполняет сеть.
2. `value` проходит через `normalize()` и denylist только в `collect()`.
3. Один record может дать несколько protocol candidates (`protocols[]`), но record rejection считается отдельно от candidate rejection.
4. Unknown protocol отклоняется, если профиль явно не задаёт default. Default никогда не угадывается по имени файла.
5. `https` field, означающий CONNECT capability, профилем может быть отображён в `http`; это не создаёт `https://` transport. Так уже сделано для GeoNode (`proxy_workbench/proxytool.py:627-652`).
6. Значения полей сохраняются только в allowlisted metadata; credentials и неизвестные blobs не попадают в report/log/cache.
7. Row-level rejection даёт partial parse, но не отменяет корректные строки. Нарушение envelope/schema, directory path, delimiter/header contract или budget — controlled error.
8. Любая операция bounded по времени между records/pages, bytes, depth, fields и total records.

### 8.2. `line` — существующие text kinds

Существующие `http`, `https`, `socks4`, `socks5`, `socks5h`, `auto`, `text`, `http-fields` регистрируются как legacy profiles и сохраняют golden behavior. `text` остаётся простым fallback для свободного текста, но формат HTML table не считается подтверждённым только потому, что regex что-то нашёл.

### 8.3. `json-records`

Один kind, разные declarative profiles. Поддерживаются только структурные операции: root `list`, fixed key `data|proxies`, protocol-keyed map и fixed path segments. Произвольные выражения запрещены.

| Profile | Наблюдаемая схема | Правила |
|---|---|---|
| `monosans-v1` | list; `protocol`, `host`, `port`; `geolocation.country.iso_code`; nested ASN; отдельный `exit_ip` | `username/password` отбрасываются; `exit_ip` не заменяет `host`; unknown `timeout` не становится latency |
| `proxifly-v1` | list; `proxy`, `protocol`, `ip`, `port`; `geolocation.country`; `https` capability | `proxy` проходит общий normalize; `https` не выбирает transport |
| `proxyscrape-v1` | list; `protocol`, `ip`, `port`, `country_code`, `anonymity`, `asn`, `last_checked`, `ssl` | Unix float seconds; ASN `AS...`; `ssl` только capability |
| `relayglass-v1` | list; `protocol`, `ip`, `port`, `country_code`, `anonymity`, `https` | JSON sample не содержит ASN/last_checked; CSV profile может читать их отдельно |
| `zevtyardt-v1` | object `{proxies:[...]}`; `ip`, `port`, `type`, `country`, `anonymity`, `last_checked` | `type=https` означает CONNECT и профилем становится `http`; body 18.5 MiB проходит отдельный fixture/budget |
| `generic-v1` | Только явно перечисленные field paths | Не используется для неизвестного JSON без preview |

Сохранённые образцы были прочитаны локально без сети. Фактически обнаружены: `monosans-json` и `new-077` — list из 650 records с `host/port/protocol`, nested ASN и отдельным `exit_ip`; `new-074` — list с `proxy/protocol/ip/port`; `new-075` — list с ProxyScrape fields и float timestamp; `new-076` — relayglass list; `new-078` — object с `proxies` и 108908 records. Это совпадает с research notes (`../proxy-sources-research-2026-09-25/notes/discovery/structured-exports.json:101-139`, `:143-180`, `:185-223`, `:228-262`, `:317-351`).

Ошибки:

- `SOURCE_INVALID_UTF8`, `SOURCE_INVALID_JSON`, `SOURCE_JSON_SHAPE`, `SOURCE_JSON_DEPTH`, `SOURCE_FIELD_MISSING`, `SOURCE_UNSUPPORTED_PROTOCOL`, `SOURCE_CREDENTIALS_PRESENT`, `SOURCE_RECORD_LIMIT` — разные code;
- неверный top-level key или protocol map не является пустым list;
- `[]`/`{"proxies":[]}` после успешного parse — `empty`, не `invalid`.

Лимиты: 32 MiB decoded body по умолчанию; max nesting 64; max string 1 MiB; max records 500 тыс.; max protocol values на record 8; общий deadline источника разделяется с HTTP. Первый adapter rollout добавляет профили по одному; ещё не реализованный профиль не может появиться в remote catalog как включённый.

### 8.4. `fields` / CSV

Вход: bounded bytes + profile. Разделители: `,`, `;`, TAB, `|`; encoding только `utf-8`/`utf-8-sig` по профилю. BOM принимается только в первом байте, CRLF обрабатывает stdlib `csv`, NUL запрещён.

Профиль задаёт одно из:

- `header=true` и exact header names;
- `header=false` и explicit column indices;
- optional `delimiter=auto`, который выбирает только из allowlist по первым 64 KiB; неоднозначность — `SOURCE_DELIMITER_AMBIGUOUS`, не догадка.

Обязательные mappings: `address` (`ip+port` или `proxy/address`), `protocol`, `country`, плюс optional metadata. Header mismatch/duplicate required column — controlled error, чтобы страна и город не поменялись местами. Неверное число полей в отдельной row — reject с частичным результатом; malformed quoting, которое сбивает stream, — error без promotion.

Начальные fixture profiles:

- `new-026`: header `ip,port,protocol,country,anonymity,uptime,last_checked`;
- ProxyScrape CSV: 13 колонок;
- relayglass CSV: 16 колонок;
- proxifly CSV: без header, `proxy,country,city`.

Исследование отдельно показывает diverging schema и timestamp formats (`../proxy-sources-research-2026-09-25/notes/discovery/structured-exports.json:127-138`, `:168-180`, `:210-223`). Поэтому профиль версиируется и не заменяется «универсальнымSniffer».

### 8.5. `page-json`

`page-json` использует record profile для каждой страницы и отдельный pagination profile. Поддерживаются declaratively:

- `page-number`: `page_param`, `start`, `step`, `limit_param/value`;
- `offset`: `offset_param`, `limit_param/value`;
- `cursor`: `cursor_param`, `cursor_response_path`, optional `next_path`;
- `next-url`: только path, extracted из response; следующий URL revalidates host/path/scheme и не может уйти на произвольный host.

Response contract: `records_path`, `page_path`, `total_path`, `has_more_path`, `next_path`. Значения читаются fixed path segments, не expression language.

Каждая страница проверяет page number, monotonic total, nonempty, repeated canonical-page hash, expected host и redirect policy. Остановки:

- `SOURCE_PAGINATION_REPEAT`;
- `SOURCE_PAGINATION_EMPTY`;
- `SOURCE_PAGINATION_TOTAL_MISMATCH`;
- `SOURCE_PAGE_LIMIT`;
- `SOURCE_PAGINATION_NEXT_INVALID`;
- `SOURCE_PAGINATION_PARTIAL`.

Сохранённые `new-045` и `new-063` имеют `{data,page,limit,total}`; первый содержит 100 rows при `total=162`, второй 50 при `total=3088`. Их fields различаются, поэтому общий envelope не означает общий record mapping.

Валидаторы ETag/Last-Modified хранятся на каждый request URL страницы. `304` страницы использует cached page только если page key и adapter profile совпадают; иначе `SOURCE_NOT_MODIFIED_WITHOUT_PAGE_CACHE`.

### 8.6. `html-table`

Внешние dependencies запрещены, поэтому используется stdlib `html.parser`; полноценный CSS engine не вводится. Это не браузер: script, event handler, iframe, external resource и JavaScript не исполняются.

Profile задаёт:

- exact `host` allowlist и `path_prefix` для каждого endpoint;
- small selector object: `tag`, `id`, exact classes, `nth_of_type`, required attribute;
- header names или column indices;
- `text|attribute` + bounded base64/plain decode для address fields;
- pagination profile из `page-json`.

Каждый redirect повторно проверяет host/path allowlist и запрет downgrade. Unknown tag/text не используется как fallback. Header fingerprint обязателен; его отсутствие/изменение — `SOURCE_HTML_SCHEMA_CHANGED`, last-good сохраняется.

Сохранённый `new-010` имеет одну `table#table_proxies`; IP и port находятся не в text cells, а в base64 `data-ip`/`data-port`, protocol/country/time — в последующих cells. Локальный HTMLParser inspection увидел 101 row и эти attributes. Профиль может читать только эти объявленные attributes/columns. Произвольные числа вне выбранных cells никогда не становятся адресами.

### 8.7. Пустая и частичная страница

- Valid line format/no records → `empty`; это не failure каталога.
- Valid table header/zero data rows → `empty`.
- Valid JSON empty container → `empty`.
- HTML shell вместо ожидаемой JSON/table → `html_placeholder`.
- Несколько страниц приняты, затем deadline/cancel/limit → `partial`; принятые candidates можно собрать, но last-good pointer не перемещается.
- Preview partial показывает данные, но не пишет их в persistent scope.

## 9. Метаданные: заявлено и измерено

### 9.1. Два независимых слоя

**Source claims** (`source_metadata`):

```text
proxy, source_id, generation_id,
country, asn, asn_org, anonymity,
source_last_checked, claimed_exit_ip,
supports_https, extra_json,
subject, declared_at, ingested_at, valid_until
```

**App measurements** остаются в `results` и provider resolver:

```text
checked_at, target samples, anonymity level/signals,
judge-observed exit_ip, exit_country, speed, reliability,
local GeoIP/ASN origin
```

Правила:

1. `country` из источника называется «заявлено источником» и хранится с `source_id`; offline GeoIP страны endpoint — «определено приложением».
2. `exit_ip` из источника — claim с `subject=exit`; он не подставляется вместо `host` и не используется как адрес проверки.
3. `last_checked` источника — claim; `checked_at` результата — timestamp нашей проверки. Они не делят одно поле.
4. `anonymity` источника не может повысить app `min_anonymity`; filter по source claim должен быть явно назван `declared_anonymity`.
5. ASN из source claim и ASN из локальной DB-IP базы имеют разные `origin`. В API добавляется `*_origin`, legacy `asn/country` сохраняется для совместимости.
6. `unknown/unknown/not tested` — значение, не `false` и не отсутствие как отрицательный факт.
7. Значения timestamp нормализуются в UTC seconds с сохранением исходной формы: ISO/fraction, Unix seconds, nanosec ISO. Невнятное значение сохраняется как отклонённые metadata; endpoint по этой причине не считается неверным.
8. ASN normalizes `AS123`, integer и вложенный object в `int`; provider name остаётся отдельным text.
9. Metadata generation удаляется вместе с cache generation; aggregate first/last membership остаётся.

### 9.2. Страна и exit

Текущий `country_resolver()` предпочитает source metadata локальному GeoIP (`proxy_workbench/proxytool.py:1383-1400`). Новый resolver возвращает tuple `(value, origin, observed_at) и имеет явный режим:

- `measured_endpoint` — локальный GeoIP адреса;
- `declared_endpoint` — source claim;
- `effective` — compatibility default, но API сообщает origin.

Country filter во всех интерфейсах принимает один и тот же `country_origin`; изменение фильтра не меняет source health/statistics.

## 10. Устойчивая загрузка, cache и last-good

### 10.1. SQLite schema v1

Новые таблицы:

```text
catalog_state
source_observation
source_generation
source_generation_entry
source_state
source_metadata
source_scan_stat
candidate_scope_exclusion
```

Ключевые поля:

- `source_observation`: run/source/feed/endpoint, start/end, HTTP/parse/cache state, status, attempts, pages, bytes, received/parsed/accepted/rejected/duplicate/blocked/new, partial, error, retryable, body SHA-256, fallback_used;
- `source_generation`: observation, `complete|partial`, `active|last_good`, created time, record count, estimated bytes;
- `source_generation_entry`: `(generation_id, proxy)` — последняя полная и, временно, partial generation;
- `source_state`: validators, final URL, last attempt/success/body/304, current/last-good generation, consecutive failures, backoff/quarantine/retry-after, last error;
- `source_scan_stat`: source + app run + profile digest + scope digest + checked/passed.

`source_metadata` Many-to-one к membership. Итоговый endpoint остаётся только в `candidates`; источники не создают parallel candidate truth.

### 10.2. Conditional GET

Для exact endpoint применяются:

```http
If-None-Match: <saved ETag>
If-Modified-Since: <saved Last-Modified>
```

Validators привязаны к source ID, endpoint ID и final URL. После redirect на другой host/path старый validator не переносится автоматически.

`304`:

- observation `http_state=not_modified`;
- обновляет только transport `last_validated_at/304_at` и сбрасывает consecutive network failures;
- не создаёт generation;
- не меняет candidates, membership, metadata, first/last-seen или app results;
- не продлевает proxy `valid_until`.

### 10.3. Last-good state machine

| Событие | Candidates/provenance | Last-good | UI state |
|---|---|---|---|
| Complete `2xx` + valid parse | commit новой membership/generation | promote | `last_good`, current |
| `304` | no data change | keep | `not_modified` |
| `2xx` empty body/valid empty container | observation без entries | keep, отметить stale | `empty` |
| `2xx` HTML placeholder | reject payload | keep | `html_placeholder` |
| invalid JSON/schema | partial records только если page уже began; иначе none | keep | `invalid` |
| timeout/cancel/limit | partial accepted records явно помечены; cache pointer не меняется | keep | `partial` |
| HTTP 429/5xx/transport | no new generation | keep | rate/network error + retry time |

Fallback URL применяется только после допустимого failure primary. Он остаётся тем же `source_id` и тем же contribution set; `endpoint_used` и `fallback_used=true` — только детали observation. Он не создаёт второй source membership и не увеличивает независимый вклад.

### 10.4. Retry, backoff и quarantine

1. `Retry-After` понимается как delta-seconds и HTTP-date. Следующая попытка не раньше max(server retry, local backoff).
2. Default backoff: 1 min, 5 min, 30 min, 2 h, 6 h; ±20% jitter. Тесты получают injectable clock/jitter.
3. Retryable: timeout/connect, 408, 425, 429, 500/502/503/504, кратковременный DNS error. `401/403`, invalid URL/destination и schema/config error автоматически не повторяются.
4. После трёх consecutive retryable failures источник получает `quarantine_until`; это не «мёртвый» и не removal. Manual `recover` очищает backoff/quarantine и запускает одну проверку.
5. Format error карантинируется по profile/config revision, а не по proxy pass rate.
6. Один failure не удаляет family и не удаляет selection.

### 10.5. Лимиты

Существующие значения остаются default: 32 MiB, 64 KiB/line, 500 тыс. candidates, 5 redirects (`proxy_workbench/proxytool.py:44-53`, `:502-514`). Добавляются:

| Ресурс | Default | Hard max |
|---|---:|---:|
| общий parallelism | 8 | 32 |
| one host | 1 | 4 |
| min interval на host | 1 s | configurable |
| request timeout | существующий source timeout | 3600 s |
| deadline всего source | 300 s | 3600 s |
| pages | 500 | 5000 |
| JSON depth | 64 | 128 |
| JSON string | 1 MiB | 16 MiB |
| CSV columns | 256 | 1024 |
| HTML nodes | 1,000,000 | 5,000,000 |
| HTML depth | 100 | 256 |
| redirect hops | 5 | 20 |

Считаются **decoded bytes**. `Content-Length` compressed body — только early hint. Chunk loop ограничивает decompressed output и compression ratio; oversized/changed Content-Length не обходит cap. Redirect уменьшает общий deadline. Stop/cancel сохранявает partial generation, но не активирует last-good.

### 10.6. Cache size и eviction

Raw body по умолчанию не сохраняется. Cache — parsed generation entries, metadata и validators, потому что raw 18.5 MiB body × множество URL быстро превысит полезный лимит.

- Global default: 256 MiB logical cache; hard 2 GiB.
- Per source: 32 MiB и максимум две generation (active + rollback/partial).
- Eviction: сначала partial, затем старый disabled/inactive; active last-good не вытесняется. Если hard limit не позволяет сохранить active generation, fetch всё равно может использоваться в текущем commit, но runtime показывает `cache_evicted` и backup не создаётся.
- `clear-data` удаляет SQLite cache/provenance вместе с results, но сохраняет `gui-settings.json` с выбором.

## 11. Provenance и статистика

### 11.1. Many-to-many

`candidate_seen(proxy, source)` эволюционирует до:

```text
(proxy, source_id, first_seen_at, last_seen_at,
 first_observation_id, last_observation_id, legacy)
```

Один endpoint может иметь много source IDs. Один publisher может иметь несколько protocol feeds, а зеркала остаются отдельными endpoint/source records, связанными только доказанным отношением. `candidate_meta.source` не является полным provenance и остаётся только compatibility first-source.

### 11.2. Счётчики одной observation

- `received` — lines/records/data rows, полученные адаптером;
- `recognized` — записи, из которых получен кандидат;
- `accepted` — canonical candidates, прошедшие normalize и policy;
- `rejected` — record/endpoint errors с reason histogram;
- `duplicates` — повтори той же canonical endpoint в этой observation;
- `duplicates_existing` — уже существовавшие до observation;
- `blocked` — отдельно от rejected;
- `new_endpoints` — фактический `INSERT` в `candidates` за collection run; один global new endpoint не суммируется как новый для двух источников;
- `pages`, `bytes`, `attempts`.

Raw/record и endpoint/canonical counters не смешиваются: одна record с двумя protocols даёт один received и два endpoint attempts.

### 11.3. Проверка приложением

`source_scan_stat` keyed by `(app_run_id, profile_digest, scope_digest, source_id)`:

- `checked_by_app` — число membership endpoints, для которых в этом profile/scope есть собственный app result;
- `passed_profile` — число прошедших `result_allowed` и selection;
- `global_unique_checked/passed` — отдельные de-duplicated числа.

`0 passed_profile` не делает источник unavailable/bad: это может быть пустой country slice, новый target, старый protocol или слишком маленькая выборка. Источник status меняют только fetch/parse/cache/adapter events.

### 11.4. Уникальный вклад в окне

Для observation window W и accepted set каждого source `Sᵢ`:

```text
exclusive_contribution(i) = |Sᵢ \ ⋃(Sⱼ, j≠i)|
leave_one_out_contribution(i) = |⋃S - ⋃(Sⱼ, j≠i)|
```

Это не зависит от порядка fetch. Identical fallback/mirror set даёт zero exclusive contribution. Если зеркало совпало только в одном snapshot, UI пишет «совпадает в срезе 2026-09-25», а не «копия» (`../proxy-sources-research-2026-09-25/01_SUMMARY.ru.md:125-140`).

Contribution — diversity metric, не quality/liveness. Reserve URL не может искусственно увеличить его. `target/country/protocol` filter задаёт `scope_digest`, но не пишет source health.

## 12. GUI: один существующий экран Sources

### 12.1. Структура

1. **Toolbar** — search по name/id/publisher/family/host, filters set/group/protocol/adapter/access/state, `Update catalog`, `Preview selected`.
2. **Cards/table** — stable ID, publisher, declared format, exact local URL (для custom), badges protocol/access/maturity, download toggle, set membership.
3. **Status columns** — HTTP state, format state, cache/last-good age, received/accepted/rejected/new exclusive, checked by app/passed profile, last error/Retry-After/quarantine.
4. **Details drawer** — evidence по каждому из пяти статусов, terms URL и даты, declared/observed metadata, history observations, cache generations, sample preview.
5. **Own lists** — текущий textarea/file import остаётся отдельным от public catalog.

Это расширение `page-sources`, а не новая параллельная страница. Существующий poll может оставаться лёгким; полный каталог загружается отдельным endpoint и обновляется после действия.

### 12.2. Preview

Preview использует тот же `collect(..., preview=True)`, те же SSRF/DNS/limits и тот же adapter, но:

- не меняет candidates, membership, metadata, settings и last-good;
- показывает accepted sample, rejected sample, reason histogram, pages/bytes/fallback;
- может использовать временный файл и удаляет его после response;
- `Commit/Update` повторно делает bounded fetch; ETag/body hash preview защищает от stale commit.

Preview не проверяет прокси и не должен называть accepted addresses рабочими.

### 12.3. Три разных действия

| Действие | Изменение | Что не меняется |
|---|---|---|
| **Pause downloads** | `download_disabled_ids += source_id` | Catalog, set membership, cache, старые candidates |
| **Remove from set** | Удаляет ID из materialized active set; source обычно не загружается следующим collect | Cache и история; нет delete candidates |
| **Exclude received addresses from scope** | Preview union membership → `candidate_scope_exclusions`; по умолчанию можно исключить только exclusive addresses, shared требуют явного confirmation | Provenance/cache остаются для undo и аудита |

В `scope_exclusions` хранятся proxy, reason, source context, created time. Scan/export исключают их, но данные не удаляются. Undo удаляет exclusion. Это не путается с глобальным denylist: denylist запрещает адрес всегда, scope exclusion — только из текущего пользовательского scope.

### 12.4. Update и recovery

- `Update catalog` показывает added/changed/retired/checksum и `selected=false` для новых ID. Он не вызывает append semantics текущего `update_sources()`.
- `Check availability/format` — preview, не proxy test.
- `Fetch selected` — обычный collect.
- `Recover` очищает quarantine/backoff и запускает одну проверку.
- Существующий `/api/sources/prune` не удаляет источник автоматически по `passed=0`; он сохраняется как compatibility route и принимает только explicit IDs/операцию remove/disable.
- `/api/sources/update` сохраняет имя, но становится catalog refresh и возвращает `new_unselected`, а не appended settings.

Термины GUI: `HTTP 2xx`, `формат распознан`, `пусто в HH:MM`, `данные из last-good, устаревают`, `прошли профиль 3/17`. Запрещены `dead source`, `working source`, `quality source` для source rows. Текущая колонка `Working` в `proxy_workbench/ui/index.html:1450-1466` переименовывается.

## 13. CLI

Существующий `collect/run/scan` остаётся. resolver источников использует settings v3. Старый `--sources PATH` сохраняется для flat-list compatibility; новый bundled catalog читается автоматически.

Рекомендуемые команды, согласованные с GUI:

```text
proxy-workbench sources list [--set quick] [--protocol socks5] [--format json]
proxy-workbench sources show <source-id>
proxy-workbench sources set quick|extended|protocol:<name>|experimental
proxy-workbench sources enable <source-id>...
proxy-workbench sources disable <source-id>...
proxy-workbench sources add <https-url> --source-format line|json-records|fields|page-json|html-table
proxy-workbench sources remove <source-id>...
proxy-workbench sources check <source-id>...          # preview availability/format
proxy-workbench sources update                       # fetch catalog, no auto-select
proxy-workbench sources recover <source-id>...
proxy-workbench sources status [--format json]
proxy-workbench sources exclude-scope <source-id> [--include-shared] [--yes]
```

`collect` дополнительно получает `--source`, `--set`, `--preview`; mutually exclusive с legacy `--sources`, чтобы не было двух authority. Все list/status команды поддерживают JSON без локализации полей и печатают русские explanatory messages через существующий `tr()` (`proxy_workbench/i18n.py:0-42`).

Exit codes: 0 success/not-modified; 1 no applicable entries; 2 validation/network/config error. Ни одна команда `sources check` не пишет в candidates.

## 14. Read-only API

Существующая access model сохраняется:

- loopback по умолчанию; non-loopback требует token (`proxy_workbench/api.py:206-209`);
- DNS-rebinding/trusted-host проверка остаётся (`proxy_workbench/api.py:240-245`);
- API остаётся `GET`/`HEAD`, без mutation (`proxy_workbench/api.py:247-299`).

Новые endpoints:

```text
GET /sources?q=&set=&protocol=&adapter=&state=&limit=&offset=
GET /sources/{source-id}
GET /source-sets
```

Ответ `/sources` содержит public catalog revision, redacted URLs, selection/pinned status и runtime aggregate. `/sources/{id}` — evidence, rights, history, cache и нормализованные declared/observed поля. Secret-like query/path удаляются через существующий `public_url()`; token в query не рекомендуется. Snapshot proxy rows и `/status` не получают mandatory fields и не меняют schema version 1.

Read-only reader открывает SQLite URI `mode=ro` и одну короткую WAL transaction; он не берёт workbench lock и не пишет. API не имеет endpoint для enable/disable/preview/update.

GUI local API остаётся отдельным loopback/token/Host/Origin-protected control plane (`proxy_workbench/gui.py:752-883`).

## 15. Порядок внедрения и критерии приёмки

### Шаг 0. Зафиксировать baseline

- Сохранить golden outputs девяти текущих kinds на сохранённых samples и текущие export fixtures.
- Зафиксировать 137 tests и smoke, а не ожидаемые 135.

**Критерий:** текущий `collect()` даёт тот же candidate set/report для всех существующих kinds; полный suite и smoke проходят. В этой design-сессии оба уже прошли.

### Шаг 1. Catalog schema и loader

- Превратить bundled `sources.json` в object schema v1.
- Добавить `source_catalog.py`, strict validator и manifest fields.
- Исправить catalog URL, не меняя `collect()`.

**Критерий:** bundled catalog проходит validator; неизвестное поле/дубликат ID/unsafe URL отклоняются до сети; 55 legacy specs имеют однозначные aliases; package-data и PyInstaller включают файл.

### Шаг 2. Settings v3 migration

- Перенести exact selection из settings v1/v2, включая custom URL.
- Materialize sets; не добавлять new IDs.
- Сохранить `use_sources` и порядок.

**Критерий:** fixtures «все 55», «один builtin удалён», «custom URL», «новый catalog ID», «use_sources=false» дают ожидаемые selections; повторная migration idempotent; atomic write не оставляет partial file.

### Шаг 3. Source DB v1 и provenance core

- Расширить `candidate_seen`, добавить observation/generation/state/metadata tables.
- Мигрировать legacy hash keys и unknown timestamps без выдуманной даты.

**Критерий:** synthetic old DB открывается и сохраняет все membership rows; `first_seen/last_seen=NULL+legacy=1` до нового наблюдения; rollback не нужен для read; два sources видны many-to-many.

### Шаг 4. Adapter kernel и `json-records`

- Подключить pure registry к тому же `collect()`.
- Добавить profile за profile: Monosans, Proxifly, ProxyScrape, relayglass, zevtyardt.
- Сохранить legacy golden outputs.

**Критерий:** каждый saved fixture даёт только canonical candidates; Monosans `host != exit_ip` не смешиваются; credentials отсутствуют; malformed rows дают partial; unsupported profile никогда не пишет candidates.

### Шаг 5. `fields` / CSV

- BOM/CRLF/delimiter/header/column mapping; RFC-style quoted fields.

**Критерий:** `new-026`, ProxyScrape, relayglass, headerless proxifly fixtures проходят; header mismatch останавливает promotion; CRLF/BOM и quoted comma проверены; SOCKS row не превращается в HTTP.

### Шаг 6. `page-json`

- Page/offset/cursor/next-url profiles и общий budget.

**Критерий:** `new-045`/`new-063` проходят two-page mock; repeat, wrong total, empty-before-total, page mismatch и max pages дают разные коды; внешний next host блокируется.

### Шаг 7. `html-table`

- stdlib parser, selector/header/attribute allowlists.

**Критерий:** `new-010` извлекает base64 `data-ip/data-port`; numeric-only false positives отсутствуют; script/JS не исполняются; другой host/path и changed header отклоняются.

### Шаг 8. Cache/resilience

- ETag/LM/304, generation promotion, last-good, Retry-After, backoff/quarantine, cache quota.

**Критерий:** local mock sequences `200→304`, `200→empty`, `200→HTML`, `200→invalid JSON`, `429→200`, `timeout→200`, `limit/cancel` дают ожидаемые states; только complete sequence меняет generation; cache eviction не удаляет active last-good.

### Шаг 9. Sets, scope exclusions и statistics

- Материализованные sets, contribution formula, scan stats, explicit exclusions.

**Критерий:** A/B permutation не меняет contribution; identical/fallback set даёт zero exclusive; country/target slice не меняет source health; disable/remove/exclude имеют разные DB effects и undo.

### Шаг 10. GUI

- Переработать существующий Sources screen/cards/drawer/preview/actions.

**Критерий:** local API tests подтверждают security; preview не меняет DB; update не выбирает new IDs; три действия показывают разные confirmations; mobile layout и RU/EN strings работают.

### Шаг 11. CLI и read-only API

- Добавить команды и GET endpoints без второго control plane.

**Критерий:** CLI/GUI/API возвращают одинаковые stable IDs/state labels; public API не имеет mutating methods; URL redact; token/host policy не ослаблены; legacy CLI flags работают.

### Шаг 12. Final regression

**Критерий:** полный suite, smoke и package install проходят; test count не уменьшается; docs и privacy/terms не переоценивают evidence.

## 16. План тестов

### 16.1. Unit

- `test_source_catalog.py`: schema, IDs, aliases, revision downgrade, unsafe URL, duplicate, unknown profile.
- `test_source_migration.py`: settings v1/v2, custom, removal, no re-add, DB legacy timestamps.
- `test_source_adapters.py`: каждый профиль, metadata path/time/ASN, credentials, empty/invalid/unsupported.
- `test_source_pagination.py`: page/offset/cursor, repeat/total/next/limits.
- `test_source_html.py`: selectors, attributes, base64, header drift, numeric false positive, no script execution.
- `test_source_cache.py`: validators, 304, last-good, Retry-After, backoff, eviction.

### 16.2. Local HTTP integration

Только `asyncio.start_server`/`http.server` на loopback с `allow_private_sources=True`, как существующие tests (`tests/test_workbench.py:151-236`, `tests/test_research_collect_integration.py:39-63`):

- redirects, DNS pinning/header, downgrade;
- compressed oversized body;
- gzip bomb/byte budget;
- 429 delta/date, 5xx, timeout, cancellation;
- host concurrency и общий parallelism;
- primary→fallback;
- complete/partial generations.

### 16.3. Provenance/statistics

- Many-to-many insertion.
- First/last-seen updates только при accepted membership.
- Raw/recognized/accepted/rejected identity.
- Duplicate vs existing duplicate.
- Fallback не увеличивает unique.
- Source set A/B order invariance.
- Shared-address exclusion и undo.

### 16.4. Interfaces

- GUI: token, exact Host, Origin, preview no-write, update no-select, three actions.
- CLI: JSON output, aliases, exit codes, old flags.
- API: GET-only, redaction, pagination, token/trusted host, no DB lock.

### 16.5. Final commands

```bash
.venv/bin/python -m unittest discover -s tests
.venv/bin/python packaging/smoke.py .venv/bin/proxy-workbench
```

После каждого schema-changing шага также выполняется targeted test, но только полный suite считается baseline gate.

## 17. Риски

| Риск | Митигация |
|---|---|
| Remote `main` catalog меняет URL и превращает fetch в SSRF/confused deputy | Data-only schema, existing `_parse_source_url`, private/metadata block, DNS pinning, no downgrade, no credentials; opt-in update |
| `304` ошибочно обнулит или «освежит» candidates | Transactional generation test; validators endpoint-specific; no membership mutation |
| Last-good скрывает исчезновение фида | Показывать age, `served_from=last_good`, observation failure; не называть current/liveness |
| HTML profile переходит на phishing/error page | Expected media type + table/header fingerprint + host/path allowlist; no JS |
| Один publisher имеет несколько feeds и зеркал | `publisher_id`, stable feed/source IDs, explicit relations; contribution по accepted set, fallback one source |
| Dynamic set expansion меняет пользовательский scope | Materialized IDs, `auto_add_new=false`; update только показывает delta |
| Cache быстро раздувает WAL | Parsed entries вместо raw body, 256 MiB default, max two generations/source, eviction и checkpoints существующего DB lifecycle |
| Source pass-rate зависит от target/filter | `source_scan_stat` keyed by profile+scope; source health не выводится из pass=0 |
| Большая миграция ломает старую DB | Nullable legacy timestamps, one transaction, mapping report, full DB fixtures |
| Старый updater/prune меняет выбор | Сначала перенаправить оба existing endpoint на новую selection service; только затем менять UI contract |
| Неясные data rights | `unknown` не разрешение; quick/extended gate; original terms URL + checked date |
| Catalog schema преждевременно считает формат доказанным | `format_confirmed` только fixture+adapter test; static evidence не runtime label |

## 18. Явно вне scope

1. Регистрация, API keys, платные/limited trial feeds и private feeds.
2. Self-hosted 3proxy/Squid/Xray/WireGuard runtime.
3. Base64 subscriptions, arbitrary YAML/JS/browser automation, MTProto и исполнение конфигураций.
4. Массовая proxy-liveness проверка источников из исследования.
5. Автоматическое включение 150/55/all sources.
6. Юридическое заключение или автоматическое выведение data license из code license.
7. Новый gateway, API authentication model, snapshot format или второй backend.
8. Автоматическое удаление старых candidates при disable/remove; для этого существует явное reversible scope exclusion.
9. Непроверенный live recheck всех 150 URL. Каталог хранит дату доказательства; текущая observe-команда проверяет только явно выбранный URL.

## 19. Итоговое решение

Следующая итерация должна менять данные и границы, а не вводить новый продукт: тот же `collect()`, тот же SQLite WAL, тот же GUI worker, тот же read-only API и тот же snapshot v1. Единственное архитектурное расширение — два pure leaf-модуля для каталога и адаптеров. Public catalog становится versioned data schema; settings v3 и source tables сохраняют пользовательский выбор; conditional GET и generations дают last-good; provenance отделяет provider claims от app measurements; четыре новых adapter kind сохраняют все существующие security limits. Внедрение идёт по профилям с fixture-gate, и каждый этап сохраняет полный suite и smoke.
