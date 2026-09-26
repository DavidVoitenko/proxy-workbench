# Handoff: area-sources (F13, F21, F27, дефект 22)

**Требования:** F13, F21, F27, дефект 22, сценарии приёмки 8, 9, 20, 21
**Контракт:** `docs/integration/CONTRACTS.ru.md`; `docs/requirements/sources-research/`
(`overlap.md`, `01_SUMMARY.ru.md`, `verification-log.md`)
**База:** `integration/ultra-2026-09-25`
**Мои файлы:** `proxy_workbench/source_catalog.py`, `proxy_workbench/source_adapters.py`,
`proxy_workbench/source_management.py`, `proxy_workbench/sourcedesk.py`,
`proxy_workbench/source-catalog.json`, `proxy_workbench/sources.json`,
`tests/test_areasources_{adapters,catalog,compare,feeds}.py`, этот файл
**Чужое рабочее дерево:** `/Users/main/Desktop/111/proxy-workbench-sources`, ветка
`sources-catalog`, HEAD `5263b55`. Только чтение, ничего там не изменено.

---

## 0. Короткий итог

| Пункт | Состояние | Где доказано |
| --- | --- | --- |
| Каталог: ID/publisher/family/mirror, 5 статусов, наборы, категории | **сделано** (150 записей, 8 наборов) | `tests/test_areasources_catalog.py` |
| Адаптеры line/JSON/CSV/page-JSON/HTML, живой сбор каждого | **сделано на уровне адаптера** | `tests/test_areasources_adapters.py` |
| Тот же сбор **движком** `proxytool.collect` | **сломано, нужен чужой файл** | §3.1 |
| 200 / 304 / пустой / битый / слишком большой / частичный | **сделано** | `tests/test_areasources_adapters.py::OutcomeTest` |
| last-good, cache TTL, ETag/304, Retry-After, backoff, карантин (в БД) | **сломано, нужны чужие файлы** | §3.2, §3.3 |
| Миграция старых URL и overrides | **сделано, найден и починен дефект** | §2.3 |
| Provenance many-to-many, first/last seen, счётчики raw/valid/duplicate/new | **сделано на чтении; запись — в движке** | §3.1 |
| Одинаковый dataset ≠ независимый издатель | **сделано, был дефект** | §2.1 |
| Метаданные поставщика ≠ наши observations | **сделано** | `test_declared_…`, `test_a_publisher_declared_country_…` |
| F27: URL/подписки, привязка, merge/replace, last-good, expiry, quota/token | **сделано** | `tests/test_areasources_feeds.py` |
| F27: секреты через references и redaction, canary отсутствует | **сделано** | `test_the_canary_is_nowhere…`, `EndToEndFeedTest` |
| F27: Clash/sing-box без исполнения rules/scripts | **сделано** | `SubscriptionImportTest` |
| F21: overlap, порядок, unknown, стоимость, bias | **сделано, найден и починен дефект** | `tests/test_areasources_compare.py` |
| Управление через GUI/CLI/API | **частично, нужно чужое** | §3.4, §3.5 |
| Таблицы `source_feed` / `membership_source` в БД | **нет, нужен `db.py`** | §3.2 |

Мои четыре тестовых файла: 127 тестов, 838 подтестов, все зелёные.
Полный прогон `-k "source or areasources"`: **378 passed, 1111 subtests passed**.

---

## 1. Что перенесено из ветки источников

Ветка `sources-catalog` содержит 8 коммитов поверх `806d13d`. На момент моего старта
интеграция уже несла `e1d57ce` (создание системы) и `8eca9a3` (сбор по выборке, адаптерные
лимиты). **Не перенесены были 4 коммита — перенесены все четыре:**

| Коммит | Что чинит | Файл |
| --- | --- | --- |
| `ef094fc` | `adapter.config` не-объект ронял каталог; `_string_list`/`_evidence_field` вместо `AttributeError`; имя пользовательского источника не терялось при миграции; порядок своих источников на экране и в фильтре | `source_catalog.py`, `source_management.py` |
| `d22c282` | `endpoint_url` поколения редактируется перед выходом из API (подпись CDN в query утекала) | `source_management.py` |
| `3f45a0d` | `migrate_settings` больше не квадратичен по выборке; `source_addresses` спрашивает индекс, а не читает всю `candidate_seen` | `source_catalog.py`, `source_management.py` |
| `5263b55` | фильтр `rights_unresolved` отвечает на тот же вопрос, что и бейдж | `source_management.py` |

`source_adapters.py` в интеграции уже совпадал с `5263b55` побайтово — переносить было нечего.
Два интеграционных отличия от чужой версии сохранены: `bundled_path()` сначала ищет
`source-catalog.json` (в интеграции каталог называется так, а `sources.json` остался старым
плоским списком URL), и `attach_contributions` проверяет `hasattr(proxytool,
'source_contributions')`, потому что движок в этой ветке её ещё не знает.

**Что осталось забрать из чужой ветки — целиком в чужих файлах, см. §3.**
`git diff --stat 806d13d 5263b55` по ним: `proxytool.py` +1956, `gui.py` +474, `api.py` +91,
`branding.py` +6, `ui/*` большой редизайн, `table-columns.json` +10, `packaging/*` +268.

---

## 2. Дефекты, найденные живым прогоном и починенные

### 2.1 Одинаковый dataset считался двумя независимыми издателями

Исследование доказало (`overlap.md`, таблица самых сильных пересечений):

```
| cur-41 | cur-43 | 21036 | 1.0 | 0 | 0 | полные наборы |
```

`hookzof/socks5_list` и `proxifly/free-proxy-list` отдают **побайтово один и тот же** набор из
21 036 адресов. В каталоге у них разные `family_id`
(`current:github:hookzof/socks5_list` и `current:github:proxifly/free-proxy-list`) и это было
единственное, на что мог опереться код. Факт присутствовал только прозой в `priority_reason`
записи `cur-41` — машиночитаемым он не был.

Вторая доказанная пара — семейство F12: `cur-11` (`dinoz0rg/proxy-list`) и `new-079`
(`dinoz0rg ... checked_proxies`) — «cur-11:0/2037, new-079:0/2037», ни у одной нет своих
адресов.

**Починено.** Введён выводимый, но задаваемый полем каталога ключ `dataset_group`:

* `source_catalog.normalize_source()` принимает `dataset_group`, по умолчанию `family_id`
  (каталог без поля ведёт себя как раньше);
* `source_catalog.dataset_group_of()` / `dataset_groups()`;
* `source_management.public_row()` отдаёт `dataset_group` в строке;
* `source_management.source_addresses(..., dataset_groups=...)` — правило «эксклюзивный вклад»
  больше не делит источник на «свою семью», а на «свой набор данных»;
* `sourcedesk._families(..., dataset_groups=...)` и `compare_sources(..., dataset_groups=...)` —
  union-find получает prior из каталога, `compare_cohorts` прокидывает `**kwargs`;
* `source-catalog.json`: `cur-41`/`cur-43` → `dataset:hookzof-proxifly-socks5-21036`,
  `cur-11`/`new-079` → `dataset:dinoz0rg-checked-2037`; ревизия `2026092501 → 2026092502`
  (у наборов обновлён `catalog_revision`).

Prior ничего не выдумывает: он только объединяет идентичности, он не создаёт адреса и не
создаёт pass-rate. `test_a_dataset_group_never_makes_a_pair_independent` фиксирует, что с
prior и без него цифры совпадают.

### 2.2 Пустой ответ 200 был неотличим от битого документа

`gproxynet/free-proxy-list` (`cur-37`) 2026-09-25T12:23:58Z отдал HTTP 200 с валидным ETag и
**пустым телом**. Для JSON/CSV/page-JSON/HTML адаптеров это давало `SOURCE_INVALID_JSON` /
`SOURCE_DELIMITER_AMBIGUOUS` / `SOURCE_HTML_PLACEHOLDER` — то есть «сломанный формат», хотя
формат никто не ломал. Молчаливый no-op выглядел как ошибка чтения.

**Починено.** `source_adapters.parse_page()` после разрешения парсера проверяет тело: пустое
или из одних пробелов — это `state='empty'`, `reason='SOURCE_EMPTY_BODY'`, для всех пяти
адаптеров одинаково. Непустое тело ведёт себя как раньше. Проверено живьём:

```
empty line            -> state=empty n=0        empty fields          -> state=empty n=0
empty json-records    -> state=empty n=0        empty html-table      -> state=empty n=0
empty page-json       -> state=empty n=0
```

### 2.3 Выключенный в старой версии источник снова включался

`migrate_settings` в ветке «до каталога» фильтровал `download_disabled_ids` по
`value not in selected`. Старый файл позволял держать источник в списке **и** выключенным
(именно так выглядит «пауза»), поэтому пауза терялась при первой же миграции: источник
молча возвращался в сбор.

Живой пример (было → стало):

```
'socks5 https://…/hookzof/…'  ->  selected ['cur-41'] disabled [] active ['cur-41']   # было
'socks5 https://…/hookzof/…'  ->  selected ['cur-41'] disabled ['cur-41'] active []    # стало
```

**Починено.** Значения из `download_disabled_ids` разрешаются через ту же таблицу алиасов, что
и список (`aliases` → `_canonical_legacy` → единственное совпадение `data_urls`), и
сохраняются, даже если источник есть и в `selected_ids`. Вычитание делает `selection_ids()`.

### 2.4 `Retry-After` из ответа провайдера не читался вообще

`quota_from_response()` разбирал `x-ratelimit-*`, `x-quota-*`, `x-plan-*` и три reset-заголовка,
но **не `Retry-After`** — единственный заголовок, который RFC 9110 требует на 429/503. Число
секунд и HTTP-дата одинаково терялись, а `plan_refresh` использует `result.retry_after` для
`next_attempt_at`, то есть отказоустойчивость опиралась на локальный backoff вместо ответа
сервера.

**Починено.** `QuotaInfo.retry_after`, `retry_after_from_response(headers, now=…)`,
разбор в `quota_from_response`, диагностика `E_SOURCE_RETRY_AFTER` с абсолютным моментом.
Обе формы RFC:

```
Retry-After: 120                        -> retry_after = now+120
Retry-After: Wed, 21 Oct 2026 07:28:00 GMT -> retry_after = now+300  (дата HTTP)
Retry-After: soon                       -> None  (не угадываем и не подставляем 0)
```

### 2.5 Цензура выдавала `rate_of_entered = 0.0` вместо «мы не смотрели»

Второе окно `survival_across_windows`, где ни один адрес не перепроверяли, давало
`rate=None` (правильно) и `rate_of_entered=0.0` (неправильно): «ноль выжил» вместо
«ноль измерен». Ровно та ошибка, ради которой функция существует.

**Починено.** `rate_of_entered` вычисляется только когда что-то было измерено
(`judged > 0`), иначе `None`.

### 2.6 У части каталога не было причины, почему сбор запрещён

`new-021`, `new-022`, `new-037` — `payload_role: proxy_list`, читаемый адаптер `line`,
`collection_allowed: false`, при этом `not_proxy_reason()` и `access_reason()` оба
возвращали `None`. Строка в GUI/API показывала «не собирается» без единого слова почему.

**Починено.** `ACCESS_REASONS['catalog_not_enabled']`, `public_row()` отдаёт
`access_blocked_code` и `collection_note` (слова самого каталога: `priority_reason`, иначе
`access_note`).

### 2.7 Статус поддержки в каталоге отсутствовал

F13 требует `supported / needs-adapter / needs-auth / unsupported / experimental`. В данных
были только разрозненные поля (`payload_role`, `adapter.kind`, `access.kind`, `priority`), а
слово `support` не встречалось в коде вообще — при том что
`tests/test_apiv1_control.py:280` уже проверяет `catalog['items'][1]['support'] ==
'needs_adapter'` (на стабе, а не на данных каталога).

**Починено.** `source_catalog.support_status()` — выводимое, не хранимое, с явным порядком
(сначала «это вообще не список», потом доступ, потом формат). Фактическое распределение по
встроенному каталогу:

```
supported 98 | needs_auth 10 | needs_adapter 15 | experimental 10 | unsupported 17
```

`needs_auth` — это ровно те 10 коммерческих/триальных провайдеров
(Bright Data, Decodo, IPRoyal, Oxylabs, ProxyRack, SOAX, Webshare и др.),
`collectable_source()` у них `False`, `access_reason()` заполнен, цены нет.
`supported` совпадает с `collectable_source()` один в один.

---

## 3. Что сломано и требует чужого файла

### 3.1 `proxytool.py` — сбор не идёт через каталог и адаптеры

**Факт.** В интеграции `proxytool.py` не импортирует ни `source_catalog`, ни
`source_adapters` (0 совпадений). `collect()` на строке 806 делает
`specs = [source_spec(url) for url in urls]`, а `SOURCE_KINDS` (строка 759) —
`('http','https','socks4','socks5','socks5h','auto','text','geonode','http-fields')`.
Из этого следует:

1. **выбор пользователя игнорируется** — `collect`/`run` всегда берут плоский список URL и
   никогда не разрешают `source_catalog.materialize_selection()`; запись, которая есть только
   в принятом удалённом каталоге, не будет скачана (это ровно тот дефект, который
   `ef094fc` описывает для чужой ветки);
2. **новые форматы нечитаемы** — `source_spec('json-records …')` падает с
   `ValueError: Неизвестный формат источника.`; 31 запись каталога имеет адаптер
   `json-records` (24) / `fields` (1) / `page-json` (3) / `html-table` (3), они недосягаемы;
3. **нет поколений, last-good, 304, кэша, карантина, provenance-счётчиков** — это DDL и код
   чужой ветки (`source_state`, `source_observation`, `source_generation`,
   `source_generation_entry`, `source_identity` в её `proxytool.py`).

**Что нужно сделать (файл чужой, я не правлю).** Взять из `5263b55` реализацию collect и
точечно сохранить интеграционные отличия:

* перед сбором разрешать выборку: `source_catalog.materialize_selection(settings,
  _sources_catalog(args))`; явный `--sources` по-прежнему важнее;
* `SOURCE_KINDS` дополнить `NEW_ADAPTERS` (`json-records`, `fields`, `page-json`,
  `html-table`) и передавать профиль в `source_adapters.parse_page`;
* на 304 обновлять `last_304_at` и **не** создавать новое поколение
  (это отдельный коммит `11f4f4`, «304 — сохранённый ответ, а не пустой»);
* запись `source_identity` для family/dataset, `source_observation` со счётчиками
  `raw/valid/duplicate/new/checked/passed`, `source_generation` + `source_generation_entry`;
* `source_contributions(db, ids)` — `source_management.attach_contributions()` уже ждёт эту
  функцию и проверяет её наличие.

**Чем я это проверил.** Живой прогон всех адаптеров у меня есть
(`tests/test_areasources_adapters.py`, локальный HTTP-сервер, реальный `httpx`-клиент,
`304`/`gzip`/`Content-Length`-обрыв/`429` — всё по-настоящему). Он доказывает, что **сам слой
адаптеров и профилей готов**. Не покрыт только слой движка: персистентность, бюджет
декомпрессии и запись наблюдений живут в `proxytool.collect`. Прогон тестов чужой ветки
(`tests/test_source_system.py`, `test_source_management.py`, `test_source_selection_collect.py`
из `5263b55`, 77 тестов) на этой интеграции даёт **46 падений**, и все они в этих
зависимостях — это готовый чек-лист приёмки.

### 3.2 `db.py` — нет миграции для таблиц фидов

`db.open_db()` после `migrate()` создаёт 23 таблицы; **ни одной** из семи нужных:

```
present: []
absent : ['source_state', 'source_observation', 'source_generation',
          'source_generation_entry', 'source_identity', 'source_feed', 'membership_source']
```

`source_feed` и `membership_source` уже объявлены в моём `sourcedesk.REQUESTED_DDL` с
комментарием «их создаёт `db.migrate()`». **Нужно:** новая миграция в `db.py`, которая
выполняет `sourcedesk.REQUESTED_DDL` (4 DDL-операции: 2 таблицы + 2 индекса) плюс DDL
поколений из чужой ветки. До этого `SourceDesk` работает только на схеме, созданной тестом, —
и это единственный честный способ его проверять сейчас. Таблицы покрыты
`tests/test_areasources_feeds.py` по объявленному контракту, а не по факту миграции.

### 3.3 `db.py` / `proxytool.py` — бюджет декомпрессии

`source_adapters` держит `max_bytes` на **декодированное** тело (`_decode` → `SOURCE_TOO_LARGE`).
Проверено: `max_bytes=1024` на теле ~24 КБ → `SOURCE_TOO_LARGE`, тело не разбирается.
Ограничение **распакованного** размера при gzip-ответе — свойства транспорта, а не парсера, и
живёт в `collect` чужой ветки. Пункт F13 «byte/page/decompression budgets» закрыт наполовину:
байтовый — да, постраничный — `next_page_url`/`page_info` (проверено: следующая страница
строится как `?page=2`, `next`-URL на чужой хост → `SOURCE_PAGINATION_NEXT_INVALID`),
декомпрессионный — в `collect`.

### 3.4 `proxytool.py` — CLI поверх каталога

В интеграции `_cmd_source` (строка 4036) — это `list | health | redact` поверх
`candidate_seen` и `sourcedesk`. В чужой ветке есть
`SOURCE_SUBCOMMANDS = ('list','show','sets','set','enable','disable','add','remove','check',
'update','recover','status','exclude-scope')` поверх `source_catalog`/`source_management`.
**Нужно:** перенести этот слой, сохранив текущие `list|health|redact` (это F27, не F13) и
русский/английский вывод. `source_management.apply_set / set_downloads / remove_sources /
select_ids / read_settings / write_settings` уже готовы и ждут вызова.

### 3.5 `api.py` — `/v1/sources` обслуживает параллельную, устаревшую модель

Факт по коду: `_op_sources_list`/`_get`/`_create`/`_update`/`_source_flag` работают со
`settings['sources']` — списком сырых URL, а не с каталогом; `_op_sources_catalog` отдаёт
`catalog.get('sources')` как есть, поэтому у записей **нет** поля `support` (проверено
чтением кода, поле не вычисляется нигде — до §2.7). `_source_refresh_state` собирает
`FeedState` из последнего отчёта collect, а не из строки `source_feed`.

**Нужно:**

1. `_op_sources_catalog` — отдавать `source_management.build_view(...)` (или добавить
   `support` и `dataset_group` в выдачу) и принять фильтры `q/set/category/protocol/format/
   access/state/limit/offset`; сейчас фильтр только `q` и по полному JSON;
2. list/get/create/update/enable/disable — поверх `source_catalog.migrate_settings` +
   `source_management.apply_set/set_downloads/remove_sources/select_ids`, чтобы
   «источник» был ID каталога, а не URL;
3. refresh/preview — читать `source_feed` (после §3.2) и отдавать
   `sourcedesk.feed_diagnostics`; сейчас там синтезированное состояние;
4. `/v1/sources` в чужой ветке — это read-only каталог с `SourceReader`; **не переносить
   слепо**: управление (`enable/disable/add/remove/update`) в интеграции уже есть, и оно
   должно остаться за `sources:write`.

### 3.6 `gui.py`, `branding.py`, `ui/*`, `table-columns.json`, `packaging/*`

Не в моей зоне. Что нужно из чужой ветки: каталоговый экран Sources (список, фильтры по
состоянию, наборы, preview, pause/resume/recover, обновление каталога) — `gui.py` +474 строки
и `ui/app.js`; подписи и терминология — `branding.py` +6; колонки — `table-columns.json`;
сборка каталога — `packaging/build_source_catalog.py`, `packaging/fill_source_sets.py`.

**Внимание про `ui/*`:** между ветками переписано ~13 000 строк `style.css` и 3 274 строки
`app.js`. Это затрагивает пользовательский дизайн, который master prompt запрещает
перерисовывать. Переносить только недостающие экраны/обработчики каталога, а не дизайн-слой.

---

## 4. Живые прогоны: что именно было выполнено

Всё ниже — локальные фикстуры и loopback-сервер. Публичные списки прокси, реальные DNSBL и
сторонние сервисы **не проверялись ни разу**. Ни один прокси не подключался. Ни одно
утверждение о качестве живых адресов не делается.

### 4.1 Живой сбор через каждый адаптер (200 и 304)

`tests/test_areasources_adapters.py::EveryAdapterCollectsTest` поднимает
`asyncio.start_server` на `127.0.0.1:0`, отдаёт тела настоящим HTTP-ответом и забирает их
`httpx.AsyncClient`. Профили взяты из встроенного каталога
(`sc._adapter_config('new-010','html-table')` и т. д.), а не выдуманы. Фактический вывод:

```
line                 state=complete n=3 first=['http://198.51.100.7:8080']  declared={}                              304=304/0b
json-records         state=complete n=2 first=['http://198.51.100.11:8080'] declared={'country':'NL','latency':120}  304=304/0b
fields               state=complete n=2 first=['http://198.51.100.31:8080'] declared={'country':'DE','anonymity':'elite'} 304=304/0b
html-table-attrs     state=complete n=1 first=['http://198.51.100.51:8080'] declared={}                              304=304/0b
html-table-columns   state=complete n=2 first=['http://198.51.100.61:8080'] declared={'country':'US'}                 304=304/0b
page-json            state=complete n=1 first=['http://198.51.100.81:8080'] declared={}                              304=304/0b
```

`declared` — это метаданные, которые **поставитель объявил**; в `value` адреса они не
попадают (`test_publisher_metadata_stays_a_claim_…`). `test_every_catalog_adapter_kind_has_a_
live_case_here` падает, если в каталоге появится адаптер, которого здесь нет.

### 4.2 Пять ответов, пять разных исходов

| Ответ | Что получено | Где |
| --- | --- | --- |
| 200 с телом | `state=complete`, адреса есть | `test_each_adapter_turns_a_live_200_into_addresses` |
| 304 | `status=304`, `content=b''` — тело не приходит и не парсится | `test_the_second_request_is_answered_with_304_and_no_body` |
| пустой 200 | `state=empty`, `reason=SOURCE_EMPTY_BODY` (после §2.2) | `test_an_empty_body_is_its_own_outcome_for_every_adapter` |
| битый | `SOURCE_INVALID_JSON`, `SOURCE_HTML_PLACEHOLDER` | `test_a_broken_document_…`, `test_an_html_page_where_json_…` |
| слишком большой | `SOURCE_TOO_LARGE` до разбора | `test_a_body_past_its_byte_budget_is_refused_before_it_is_read` |
| обрыв на середине | `httpx.RemoteProtocolError` — до адаптера не доходит | `test_a_truncated_transfer_never_reaches_the_adapter` |
| частичный (лимит записей) | `state=partial`, 100 записей, `truncated=True`, `reason=SOURCE_RECORD_LIMIT` | `test_a_list_past_the_record_cap_is_partial_and_keeps_what_it_read` |
| 429 | тело `slow down` не парсится как список | `test_a_rate_limited_response_carries_no_body_to_parse` |

Форма GFP-агрегатора (большой список, обрезанный по капy) отрабатывает как `partial` с
сохранением прочитанного — это и есть требование «memory/budget limits и partial report»
для набора в 506 267 адресов.

### 4.3 F27: жизненный цикл фида целиком

`tests/test_areasources_feeds.py` (42 теста) на реальной схеме из
`sourcedesk.REQUESTED_DDL`:

* bind → refresh `ok` → `active = last_good = 5 адресов`, `expires_at = fetched_at + ttl`;
* **ошибка обновления не очищает рабочую коллекцию** — проверено для `unavailable`,
  `rate_limited`, `token_expired`, `quota_exhausted`, `invalid`, `too_large`: ни один не
  удалил ни одну строку, ни один не продлил `expires_at`;
* пустой ответ и частичный ответ — не удаления;
* `304` — только валидация транспорта: ни добавлений, ни удалений, ни продления срока;
* delta добавляет и сохраняет, не заявляя удалений; `replace` удаляет только то, что этот
  источник перестал отдавать, и **чужое membership не трогает**:
  `membership('other-feed') == (ENDPOINTS[4], other)` после replace;
* пересечение показывается отдельно (`retained_shared`);
* backoff растёт (60 → 300 → 1800 → 7200 → 21600), `Retry-After` провайдера никогда не
  перебивается меньшим сроком; три отказа подряд → карантин, хороший ответ его снимает;
* expiry/stale/dropped — три разных сообщения в разные моменты;
* **смена credentials** увеличивает `access_revision` и называет ровно то, что перестаёт быть
  доказательством: `invalidates: [["acc-7", 1]]`; попытка сменить `access_id` — отказ
  `E_CONFLICT_REVISION`;
* canary `canary-SUPERSECRET-9f2b7c` не найден ни в `public_view`, ни в строках `source_feed`,
  ни в диагностике, ни в сериализованном плане (проверено `find_secret_leaks`);
* Clash: `proxies` → только `http`/`socks5`; `vmess` → `E_IMPORT_FORMAT`; запись с
  `username/password` → **отказ целиком** `E_SECRET_CREDENTIALS` (импортировать её означало бы
  сохранить чужой секрет в своей базе); `rules`, `rule-providers`, `proxy-groups`, `script`
  только в `ignored`;
* sing-box: `http`/`socks` → адреса, `version: '4'` → `socks4`, `vmess` отклонён,
  `direct`/`block` в `ignored`, `route`/`dns`/`experimental` в `ignored`;
* превышение `max_bytes` — отдельный исход `limit_exceeded`, а не «неправильный формат».

### 4.4 F21: главный сценарий и остальные требования

`tests/test_areasources_compare.py` (28 тестов) на реальной миграции `db.open_db()` с
наблюдениями, записанными через настоящий `INSERT INTO observations`:

**Два издателя отдают ОДИНАКОВЫЙ адрес** (форма `hookzof ≡ proxifly`):

```
overlap identical=True, shared=20, jaccard=1.0, left_only=0, right_only=0
pub-a: offered=20 admitted=12 unique_offered=0 unique_admitted=0
pub-b: offered=20 admitted=12 unique_offered=0 unique_admitted=0
bias BIAS_SHARED_COST: '…это один издатель, посчитанный дважды, а не два независимых'
семейств: один, members ('pub-a','pub-b'), identical_group=True
```

Третий издатель с 8 собственными адресами получает `unique_offered=8`, `unique_admitted=3`;
у первых двух `unique_offered=0`. Не собивавшийся источник — `status=not_collected`,
`reliability=None`, а не ноль.

* **Перестановка порядка** — все 24 перестановки четырёх источников дают побайтово одинаковый
  `as_dict()`; повтор источника в списке ничего не меняет.
* **Unknown ≠ ноль** — измерение с `TARGET_UNAVAILABLE` даёт `unknown=1` и не входит в
  знаменатель: `reliability = 3/(3+4)`, не `3/8`.
* **Одинаковый dataset** — prior каталога не меняет ни одной цифры и не создаёт адресов.
* **Стоимость до одного пригодного адреса** — `admitted=12, bytes=12*2*1024, seconds>0,
  attempts=12*2`; у источника без пригодных адресов все поля `None` и есть текст причины.
* **Bias** — фильтры читаются из `job.scope_json` реально отработавшего задания
  (`BIAS_FILTERS` с `countries`), география публикатора отделена от наших измерений
  (`publisher_country_claims > 0`, `measured_countries == 0`, `BIAS_PUBLISHER_GEO`), все коды
  объявлены в `BIAS_CODES`.
* **Survival** — окно без перепроверок даёт `censored=12, dead=0, rate=None` и
  `rate_of_entered=None` (после §2.5), а не «все умерли».
* **Когорты** — три предупреждения (ревизия профиля, профиль, порог приёмки) и два
  разбиения вместо одного среднего.
* **Провайдеры** — `provider_inventory` отдаёт только непригодные для сбора записи, у всех
  `status=not_collected`, в тексте нет ни `$`, ни `USD`.

### 4.5 Миграция старого списка (сценарий «миграция старых URL/overrides»)

`tests/test_areasources_catalog.py::MigrationTest` на реальных строках
`proxy_workbench/sources.json` (старый плоский список из 57 URL):

* каждый старый URL сохраняет **точную** строку в `specs` (`{'cur-01': 'https://raw.githubusercontent.com/MuRongPIG/…'}`);
* известный URL → стабильный ID (`cur-01`, `cur-41`, `cur-02`, `cur-52`, `cur-32`);
* неизвестный URL → `custom-*`, остаётся собираемым;
* миграция идемпотентна (`migrate(migrate(x)) == migrate(x)`);
* выбранный пользователем формат (`socks5 URL`) переживает повторную миграцию;
* пауза из старой версии выживает в обеих записях (URL и ID) — §2.3;
* имя пользовательского источника не теряется;
* `apply_set`/`remove_ids` меняют только выборку, ревизия каталога не трогается.

---

## 5. Состояние каталога (факты, не оценки)

* 150 записей, 8 наборов (`quick` 8, `extended` 69, `experimental` 21, 4 протокольных,
  `custom`); ревизия `2026092502`.
* `collectable_source()` — 98 записей; ровно они получают `support='supported'`.
* Статусы поддержки: 98 supported / 10 needs_auth / 15 needs_adapter / 10 experimental /
  17 unsupported.
* `not_proxy_reason`: 108 «список», 15 «нет адаптера», 11 «конфигурация подписки»,
  10 «коммерческая страница», 6 «собственная инфраструктура».
* Ни у одной записи `evidence.proxy_liveness.state != 'not_run'` — исследование не открывало
  ни одного соединения через найденные адреса, и каталог этого не скрывает.
* GFP-агрегатор `new-043` — `category=aggregator`, `priority=medium`, собираем, лимиты и
  partial-отчёт — дело бюджета, а не обещания.

---

## 6. Чего я сознательно не делал

* Не проверял ни одного публичного адреса, реального DNSBL и стороннего сервиса.
* Не перерисовывал UI и не переносил дизайн-слой из чужой ветки — только описал (§3.6).
* Не добавлял провайдеров, требующих ключ: они видимы, инертны, с честным статусом и ссылкой
  на условия, и по умолчанию не включены.
* Не заявлял качество живых прокси: ни одного измерения живого адреса в этой работе нет.

---

## 7. Быстрый чек-лист приёмки после чужой правки

1. `python3 -m pytest tests/ -q -k "source or areasources"` → 378 passed.
2. Прогон тестов чужой ветки (`test_source_system.py`, `test_source_management.py`,
   `test_source_selection_collect.py` из `5263b55`) → 0 падений вместо текущих 46.
3. `python3 -c "from proxy_workbench import db; ..."` → в таблице есть `source_state`,
   `source_observation`, `source_generation`, `source_identity`, `source_feed`,
   `membership_source`.
4. `proxy-workbench source list` показывает строки каталога, а не URL из плоского списка.
5. `GET /v1/sources/catalog` отдаёт `support` и `dataset_group` в каждой записи.
6. `curl -H 'If-None-Match: "…"'` по живому источнику → 304 и **не** новое поколение.
7. Источник с `Retry-After` → `next_attempt_at` не раньше ответа сервера.
