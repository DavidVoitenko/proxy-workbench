# F21 — сравнение источников и поставщиков: что реализовано и куда это подключить

Файл-владелец: `proxy_workbench/sourcedesk.py` (раздел «Comparison of sources and
suppliers (F21)», примерно строки 1660–2600).
Тесты: `tests/test_sourcedesk_compare.py`.
Чужие файлы не редактировались. Ниже — точные места и сигнатуры, если кто-то
захочет довести функцию до CLI/GUI.

---

## 1. Что сделано

Сравнение считает **только по тому, что приложение само наблюдало**. Оно читает:

| Таблица | Что берёт |
| --- | --- |
| `candidate_seen(source, endpoint_id)` | какой каталоговый издатель отдал адрес (многие-ко-многим) |
| `membership_source(source_id, endpoint_id, collection_id)` | какой пользовательский фид (поставщик) отдал адрес |
| `observations(...)` | что реально измеряли, когда, для какой ревизии профиля, и что вернулось |
| `endpoints(country, country_source)` | географию **и то, чьё это наблюдение** |
| `job(scope_json, state)` | условия прогона: find-N, фильтры, бюджеты |
| `source_feed(last_outcome, etag, …)` | fetch health пользовательских фидов |

### Публичный API

```python
sd.Cohort(start, end, profile_id=..., profile_revision=..., collection_id=...,
          min_success=2/3, access_ids=(), label='')

sd.compare_sources(conn, *, sources, cohort,
                   family_jaccard=1.0, sample_floor=20, with_survival=False)
    -> SourceComparison   # rows, overlaps, families, cost, biases, survival, warnings

sd.compare_suppliers(conn, left, right, *, cohort, **kwargs)
    -> SupplierComparison  # два своих поставщика на одних условиях + equal_terms

sd.compare_cohorts(conn, *, sources, cohorts)
    -> (tuple[SourceComparison, ...], tuple[str, ...])   # breakdown + предупреждение несопоставимости

sd.survival_across_windows(conn, *, sources, profile_id='', profile_revision=1,
                           collection_id='', windows=None, min_success=2/3,
                           count=3, start=None, end=None)
    -> tuple[SurvivalStep, ...]

sd.provider_inventory(catalog, *, comparison=None) -> tuple[ProviderNote, ...]

sd.wilson_interval(successes, trials, *, z=1.959963984540054) -> (low, high)
sd.classify_measurement(payload, error_code=None) -> ('pass'|'fail'|'unknown', reason)
```

У всех объектов есть `as_dict()` — для JSON в GUI/API без знания внутренних полей.

---

## 2. Требования честности — как они обеспечены

### 2.1 `unknown` не превращается в нулевую надёжность

`classify_measurement` даёт **три** исхода, а не два:

* `pass` — verdict есть и `min_target_reliability > 0`;
* `fail` — есть код, который означает отказ самого адреса
  (`UNREACHABLE`, `CONNECT_TIMEOUT`, `READ_TIMEOUT`, …), **или** verdict честно нулевой;
* `unknown` — вывод не состоялся: цель была недоступна, бюджет кончился, задание
  остановили, verdict потерялся или нечитаем.

`unknown` **не входит в знаменатель** `reliability`, но показывается отдельным
счётчиком и попадает в bias-метку `unknown_measurements`.

Набор «не-conclusive» кодов не скопирован, а подмешивается из
`probes.TARGET_FAILURE_CODES` на лениво (`_non_conclusive_codes()`), чтобы
определения не разъехались: сбой на стороне цели — это свидетельство о цели,
а не о прокси.

Три разных состояния строки (`SourceStats.status`):

| status | смысл |
| --- | --- |
| `measured` | есть хотя бы одно conclusive-измерение |
| `no_data` | адреса есть, измерений в этом окне нет |
| `not_collected` | адресов этого источника в базе нет вообще |

Изменение фильтра/ревизии профиля **не** объявляет источник мёртвым: адреса
остаются, `offered` не меняется, статус становится `no_data`, а не `fail`.

### 2.2 Пересечение не считается независимостью

* `overlaps` — все пары с общим адресом, с `shared`, `jaccard`, `only_left/right`
  и флагом `identical` (наборы равны).
* `families` — union-find по парам с `jaccard >= family_jaccard`
  (по умолчанию `1.0`, то есть **только точные копии**). Цепочка почти-дублей
  схлопывается в одно семейство: зеркало зеркала — всё ещё зеркало.
* `Family.unique_endpoints` считается против **всех остальных источников
  сравнения**, а не только против соседей по семейству.
* `SourceStats.unique_offered` / `unique_admitted` — маргинальный вклад: то,
  что источник приносит сверх всех прочих.

Если два издателя отдали одинаковый набор, у обоих `unique_offered == 0` —
по построению, а не по эвристике.

### 2.3 Порядок источников не ломает сопоставимость

Все операции — над множествами `endpoint_id`, все выходные списки отсортированы
по `source_id`. Проверено перебором всех 6 перестановок: `as_dict()` совпадает
байт в байт. Повтор одного и того же id в списке тоже ничего не меняет.

### 2.4 Find-N, фильтры, география

Читаются из `job.scope_json` тех заданий, чьи `observations` попали в когорту,
а не из текущих настроек (настройки могли измениться после прогона).

Bias-коды (`sd.BIAS_CODES`): `find_n`, `filters`, `geography`, `order`,
`sample_size`, `publisher_geography`, `shared_cost`, `unknown_measurements`, `scope`.
Каждая метка ставится только когда база реально показывает это условие.

Отдельно: `endpoints.country_source`. Если страна пришла из
`'source'`/`'provider'`, это **заявление издателя**, а не наше измерение.
Счётчики `publisher_country_claims` и `measured_countries` в строке их не смешивают.

### 2.5 Выборка и confidence

`reliability` + интервал Уilson (`reliability_low`/`high`) + `sample_sufficient`
(порог `sample_floor`, по умолчанию 20 conclusive-измерений). Малая выборка не
прячется — она помечается и widens интервал.

### 2.6 Стоимость до одного пригодного адреса

`CostPerAdmitted`: `seconds`, `bytes`, `attempts` на один `admitted`.
* `admitted` — адрес, чьё conclusive-измерение прошло порог `min_success`
  когорты.
* Секунды — `finished_at - started_at` наблюдений; байты — из
  `samples[].bytes` + `speed.bytes`; попытки — `len(samples)`/`requests`.
* Если пригодных адресов **ноль** — все поля `None` и заполнен `reason`.
  Не ноль и не бесконечность: «цена одного пригодного адреса, которого не
  было» — это не число.

Известная и явная поправка: измерение общего адреса charged каждому
кредитованному источнику, поэтому стоимость семейства — стоимость всего
семейства. Это отмечено bias-кодом `shared_cost`.

### 2.7 Пустой gproxynet — отдельный исход, а не успех

`FetchState.delivered_nothing` — отдельное состояние. URL, ответивший
`HTTP 200` с валидным `ETag` и **пустым телом**, даёт
`last_outcome='empty'`, `delivered_nothing=True` — и это не то же самое, что
`last_outcome='ok'`. (Коды исходов уже существовали: `REASON_CODES['empty'] =
'empty_no_removal'`.)

### 2.8 Коммерческие и trial — видны, но инертны

`provider_inventory(catalog)` возвращает записи со
`access.kind in ('paid','temporary_trial','free_with_api_key')` **и** всё, что
закрыто `collection_allowed=False`. У всех `status='not_collected'`,
`collectable=False`, есть `terms_url`. **Цены нет** — ни в `as_dict()`, ни в
тексте: тариф, на который приложение не подписывалось, оценивать нельзя.

На реальном каталоге (150 записей) это 52 записи, включая все семь названных:
`new-001` Webshare, `new-002` Bright Data, `new-003` Decodo, `new-004` IPRoyal,
`new-005` SOAX, `new-006` Oxylabs, `new-007` ProxyRack.

---

## 3. Где вызывать (НЕ правил — только точные места)

### 3.1 API (`proxy_workbench/apiv1.py` + `proxy_workbench/api.py`)

Маршруты для источников объявлены блоком `# --- sources ---` около
`apiv1.py:1229`. Добавить рядом:

```python
Route('GET', '/v1/sources/comparison', 'sources.comparison', 'sources.read',
      'сравнение источников в одной когорте', query=COMPARISON_QUERY,
      tags=('sources',)),
```

Обработчик в `api.py` рядом с `_op_sources_list` (`api.py:2010`). Имя
операции резолвится как `'_op_' + operation.replace('.', '_')` (`api.py:648`),
то есть нужен метод:

```python
def _op_sources_comparison(self, call):
    from . import sourcedesk
    params = call.params
    cohort = sourcedesk.Cohort(
        start=float(params['start']), end=float(params['end']),
        profile_id=params.get('profile_id', ''),
        profile_revision=int(params.get('profile_revision', 1)),
        collection_id=params.get('collection_id', ''))
    report = sourcedesk.compare_sources(self.connection(), sources=params.get('source') or (),
                                       cohort=cohort)
    return {'items': [row.as_dict() for row in report.rows], ...}
```

Отдавать `report.as_dict()` целиком проще: `rows`, `overlaps`, `families`,
`cost`, `biases`, `warnings` уже сериализованы.

`query=` описать как `Field('source', 'string')` (повторяемый) плюс
`start`/`end`/`profile_id`/`profile_revision`/`collection_id`.

### 3.2 GUI (`proxy_workbench/gui.py`)

Диспетчер POST — блок `if path == '/api/sources/...'` около `gui.py:4038`.
Добавить:

```python
if path == '/api/sources/compare':
    return self.respond(200, self.app.compare_sources(payload))
```

Метод в классе приложения рядом с `preview_source`:

```python
def compare_sources(self, payload):
    from . import sourcedesk
    report = sourcedesk.compare_sources(
        self.conn, sources=payload.get('source') or (),
        cohort=sourcedesk.Cohort(**payload['cohort']))
    return report.as_dict()
```

В таблице сравнения обязательно показывать `biases` и `warnings` рядом с
цифрами, а `reliability=None` рендерить как «нет данных», не как `0 %`.
Столбец «уникальный вклад» брать из `row['unique_offered']` / `['unique_admitted']`,
а `family_id` — чтобы копии were видны группой.

### 3.3 CLI (`proxy_workbench/proxytool.py`)

Рядом с существующей командой `sources`. Сигнатура, которую стоит вызвать:

```python
sub = p.add_subparsers(dest='command')
compare = sub.add_parser('sources-compare')
compare.add_argument('--source', action='append', dest='source', required=True)
compare.add_argument('--start', type=float, required=True)
compare.add_argument('--end', type=float, required=True)
compare.add_argument('--profile-id', default='')
compare.add_argument('--profile-revision', type=int, default=1)
compare.add_argument('--collection-id', default='')
compare.add_argument('--min-success', type=float, default=2 / 3)
compare.add_argument('--family-jaccard', type=float, default=1.0)
compare.add_argument('--json', action='store_true')
```

и в `_cmd_*`:

```python
report = sourcedesk.compare_sources(db, sources=args.source, cohort=cohort)
print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2) if args.json
      else _render_source_comparison(report))
```

Рендерер обязан печатать `warnings` и `biases`; иначе отчёт врёт по умолчанию.

---

## 4. Что осталось за пределами этого файла

1. **`db.py` не создаёт `source_feed` и `membership_source`.** Они объявлены в
   `sourcedesk.REQUESTED_DDL` (это константа DDL, модуль её не исполняет), но
   миграции 15 в `db.py` касаются только `results`. На реальной базе
   `data/proxies.sqlite3` (версия 14) этих таблиц нет. Функция это переживает
   корректно: `_table_exists` их проверяет, атрибуция ограничивается
   `candidate_seen`, fetch health просто отсутствует. **Нужно:** либо добавить
   миграцию, которая исполняет `REQUESTED_DDL`, либо решить, что пользовательские
   фиды заводятся только в тестах. Сравнение сейчас работает, но поставщики из
   `membership_source` не увидят данных, пока таблицы нет.

2. **`candidate_seen` без времени.** В таблице `candidate_seen(proxy, source,
   endpoint_id)` нет колонки времени, поэтому «когда источник отдал адрес»
   неизвестно; окно применяется к измерениям. Для когорт по времени публикации
   нужна миграция с `added_at`/`last_seen_at` (как в `membership_source`).
   Пока этого нет, `universe='compared'` — уникальность считается против
   источников **выбранного сравнения**, а не против всей таблицы. Это указано в
   `SourceComparison.universe`; если нужно против всей таблицы, расширять
   `_attribution()`.

3. **`observations` не хранит `collection_id` и `network_id`.** Когорта по
   коллекции сейчас выражается через `membership_source`; по сети (`Scope.network_id`)
   в таблице наблюдений поля нет — сопоставимость по сети проверить нечем, и она
   не проверяется. Честное пробел, а не «проверено и совпало».

4. **`job.scope_json` — единственный источник сведений о find-N.** У pipeline
   есть `FindPolicy`, но в задание он попадает только как `filters.want` /
   `filters.count_what` (`proxytool.py:4513`). Если другой путь создаёт задание,
   метка `find_n` не появится — это «нет свидетельства», а не «find-N не было».

5. **Стоимость в байтах и попытках — из `observations.verdict`.** Поле
   `speed.bytes`/`samples[].bytes` заполняется только если скорость/сэмплы
   реально измерялись. Адрес, упавший на TCP, в байтах не charged. Это осознанно
   (совпадает с `proxytool.measurement_bytes`), но означает: у плохого источника
   стоимость на пригодный адрес выглядит низкой, потому что попытки были
   дешёвыми, а не потому что он хорош. UI обязан это подписать.

6. **Ключ `source` в `candidate_seen` — это `source_key(url)`
   (`proxytool.py:738`, sha256 от URL, 16 hex), а не id каталога (`cur-41`).**
   То есть при выборе источников для сравнения надо передавать то, что реально
   лежит в колонке. Для каталоговых источников это ключ от URL из
   `sources.json`; для пользовательских фидов — `membership_source.source_id`.
   Удобной обвязки «id каталога → ключ в БД» в проекте нет; если она нужна
   GUI, её надо добавить в `source_catalog.py` (чужой файл — не делал).

---

## 5. Как проверялось

На временной базе, созданной **настоящим** `db.open_db()` (то есть
собственный мигратор проекта), с наблюдениями, записанными в реальную схему.
Фикстура намеренно различающаяся:

* `pub-A` и `pub-B` отдают **идентичные** 30 адресов (копия);
* `pub-C` отдаёт 12 адресов, которых нет ни у кого;
* 4 окна по часу; 26 переживают, 6 умирают в третьем окне;
* у `pub-C` два наблюдения не-conclusive (`TARGET_UNAVAILABLE`) — они не должны
  стать нулём;
* задание с `want=40` и `countries=['NL']` — чтобы метки `find_n`/`filters`
  опирались на данные, а не на константу;
* страны `NL` помечены `country_source='geoip'` (наши), `DE` — `'source'`
  (заявление издателя).

Пройденные сценарии:

| Сценарий | Результат |
| --- | --- |
| `pub-A` vs `pub-B`: `identical=True`, `shared=30`, `jaccard=1.0` | ✅ |
| `pub-B.unique_offered == 0`, `unique_admitted == 0` | ✅ |
| оба в одном семействе `identical_group=True` | ✅ |
| `pub-C.unique_offered == 12`, `unique_admitted == 4` | ✅ |
| все 6 перестановок порядка дают идентичный `as_dict()` | ✅ |
| `pub-C`: pass=4, fail=6, unknown=2; `reliability = 4/10`, не `4/12` | ✅ |
| окно без наблюдений: `reliability=None`, `status=no_data`, `cost=None` + причина | ✅ |
| `paid-brightdata`: `status=not_collected`, `reliability=None` | ✅ |
| survival: entered 30→30→30→24, `dead=6` в третьем окне | ✅ |
| окно без замеров: `censored=4`, `dead=0`, `rate=None` | ✅ |
| gproxynet: `last_outcome='empty'` + валидный ETag → `delivered_nothing=True` | ✅ |
| `compare_cohorts` с разными ревизиями/профилями/порогами/непересекающимися окнами → 4 предупреждения | ✅ |
| 7 коммерческих поставщиков видимы, `not_collected`, без цены | ✅ |
| **на реальной `data/proxies.sqlite3`** (0 наблюдений): `has_data=False`, `reliability=None`, предупреждение «pass-rate неизвестен, а не нулевой» | ✅ |

`tests/test_sourcedesk_compare.py`: 43 теста, 65 subtest — зелёные.

**Чего эти цифры не говорят.** Ни одно измерение в фикстуре не является
измерением живого публичного прокси. Все числа выше — про то, как функция
обращается с записанными наблюдениями. Какой процент реальных публичных
прокси выживает, по этому репозиторию установить нельзя: в `data/proxies.sqlite3`
`observations` и `results` пусты (0 строк), и функция это честно сообщает, а не
подставляет правдоподобную цифру.
