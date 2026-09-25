# Handoff: geo

**Требования:** F08 (география и характеристики); частично — приёмка F08 «GUI/API/export применяют один критерий страны, read-only фильтр сам не запускает сеть, неизвестное не выдаётся за проверенное» и R-строка 37–38 REVIEW.
**Контракт:** `CONTRACTS.ru.md` §4.4 (поля строки), §1.1 (endpoint/observation), §3.3 миграции 1, 4, 7, §5.4 (коды причин), §5.5 (единицы) — версия 1.
**База:** ветка `integration/ultra-2026-09-25`, HEAD `47229d0` на момент написания.

Владение: `proxy_workbench/geo.py` и `tests/test_geo_*.py`. Чужие файлы не редактировались, DDL не пишется, сеть не используется.

---

## 1. Прошу внести в чужие файлы

### 1.1 `proxy_workbench/proxytool.py` — подключить модуль к движку

| Место | Что внести | Зачем |
| --- | --- | --- |
| `country_resolver` (`proxytool.py:1528-1536`) и `provider_resolver` (`:1613-1623`) | Собрать один `geo.Resolver(country_db=geo, provider_db=asn, declared=dict(meta), now=…, database_version=…)` и передавать его вместо двух замыканий. `geoip.CountryDB`/`AsnDB` остаются теми же объектами — модуль их не дублирует | §1.2: нормализация и чтение баз в одном месте; источник и дата знания больше не теряются по дороге |
| `matches_selection` (`:1635-1648`) и `in_scope` (`:1721-1726`) | Страну определять одним вызовом `geo.evaluate(criterion, endpoint_fact, exit_fact, now)` вместо `row_country(...) in countries` | F08: один критерий в GUI, API и export; сегодня критериев три и разные |
| `export`, формирование строки (`:1818-1826`) | Дописать в публикуемую строку поля из `Resolver.describe()`: `country_source`, `country_at`, `country_age_seconds`, `country_stale`, `exit_country_source`, `exit_country_at`, `hosting_basis`, `ip_version`, `cidr`, `target_capabilities`, `country_reason` | §4.4 перечисляет `country/exit_country/asn/provider/hosting/cidr` без происхождения; F08 требует происхождение и дату. Существующие имена не переименовываются, только добавляются |
| `update-geoip` (`:2350-2370`) и `download_geoip` (`:2108-2132`) | После успешной загрузки вызвать `geo.install_database(path, body, kind, month, geoip.validate_download)` вместо `temp.write_bytes` + `replace` (`:2128-2130`), а статус отдавать через `geo.geo_status(args.data, now, country_db, asn_db)` | Версия и дата установленной базы становятся известны; повреждённое обновление не стирает рабочую базу (сейчас при `validate` исключение и до записи, но версия не пишется вовсе) |
| CLI (`:2064-2068`, `main` `:2292`) | Принять `--country-basis {endpoint,exit,either}`, `--country-unknown {exclude,include_unverified,require_measurement}`, `--country-exclude`, `--country-max-age`; существующий `--country` не менять | F08: включения/исключения и явная политика unknown должны доехать из CLI без правки существующего флага |
| `scan` (`:1283-1311`) | Для `basis=exit/either` не отбрасывать кандидата по стране endpoint; вместо отбрасывания планировать judge-пробу: `geo.plan_measurement(...)` → `judge_required` | MASTER-PROMPT F08: «Endpoint DE не отбрасывать заранее только из-за желаемого выхода NL» |

### 1.2 `proxy_workbench/api.py` и `proxy_workbench/gui.py` — одна выдача, один статус

- `api.public_row` (`api.py:51-89`) и `gui.App.results` (`gui.py:620-761`, следующий метод начинается на `gui.py:764`): заменить собственный отбор по стране на `geo.filter_rows(rows, criterion, resolver, now)`. Обе поверхности получают `FilterResult.as_dict()` для агрегатов и один и тот же `criterion_digest`; это то, что проверяет `tests/test_parity` (§2.3).
- `gui.App.geo_status` (`gui.py:557-562`): вернуть `geo.geo_status(...)`. Текущий ответ не содержит версии и возраста базы, поэтому пользователь не может отличить «база January» от «база September».
- `gui` маршрут `/api/countries`: отдать `geo.country_list()` — тот же список, что сейчас литералом в `ui/app.js:1565`, — чтобы picker и backend разошлись только вместе с падающим тестом `tests/test_geo_picker_parity.py`.

### 1.3 `db.py` — три колонки и номера миграций

DDL не пишу. Проверено чтением `db.py` на текущем HEAD: миграция 1 (`db.py:575-588`) объявляет `ip_version`, `country`, `country_source`, `country_at`, `asn`, `provider`, `hosting`, `cidr` — этого набора моему модулю хватает, и он заполняет его как есть. Не хватает трёх вещей:

| Таблица | Колонка | Где сейчас | Обоснование |
| --- | --- | --- | --- |
| `endpoints` (миграция 1) | `hosting_basis TEXT` | в `_m1` (`db.py:574-588`) только `hosting INTEGER` | F08 и `08-critical-synthesis.ru.md:42`: regexp по имени организации не доказывает residential. Без этой колонки потребитель прочитает `hosting=0` как «не hosting, значит residential» |
| `observations` (миграция 4) | `exit_ip TEXT`, `exit_country TEXT`, `exit_country_source TEXT`, `exit_country_at REAL` | `_m4` (`db.py:665-678`) таких колонок не имеет | Наблюдаемый выход — свойство замера, а не endpoint; §4.4 публикует `exit_ip`/`exit_country` в строке, а хранить их негде |
| `pools` (миграция 7) | `quota_basis TEXT` (`endpoints`\|`exit_ips`) | `_m7` (`db.py:732-735`) есть только `policy_json` | Основание квоты обязано быть полем, а не выводом из наличия `exit_ip`. Допустимо положить его в `policy_json` — тогда правки схемы не нужно, но имя обязано быть зафиксировано |

### 1.4 `maintenance.py` — правка не требуется

`RUNTIME_DIRS = ("exports",)` (`maintenance.py:32`), поэтому `clear_runtime` не трогает `data/geoip/` — ни базы, ни новый sidecar `<database>.meta.json`. Отдельно добавлять его в `RUNTIME_FILES` не нужно и не нужно: это кэш геоданных, а не runtime-состояние проверки.

---

## 2. Что уже сделано у меня

Публичный API `proxy_workbench/geo.py`:

**Знания и их происхождение**
- `CountryFact(code, source, at, database_version, address)` с `.known`, `.age_seconds(now)`, `.is_stale(now, max_age)`, `.as_dict(...)`; `.of(code, source, …)` — для данных (некорректный код становится unknown, а не исключением), `parse_criterion` — для ввода пользователя (строгий, с русским сообщением).
- `merge_facts(facts, now, max_age) -> MergedCountry(fact, others, conflict)` — приоритет по источнику (`resolved > observed_exit > geoip > source > provider`), расхождение не теряется, а называется.
- `Resolver(country_db, provider_db, declared, now, database_version, max_age_seconds)` с `endpoint_fact(proxy, resolved_ip=, resolved_at=)`, `exit_fact(exit_ip, at=)`, `provider_fact(proxy)`, `from_row(row)`, `describe(row, criterion, now)`. Оборачивает существующие `geoip.CountryDB`/`AsnDB`, сети не касается.

**Критерий и решение**
- `parse_criterion(value, basis=, unknown=, max_age_seconds=, include=, exclude=)` — строка пикера `"de,NL"`, список, либо mapping (ключи `country`/`countries`, `country_basis`, `country_exclude`, `country_unknown`, `country_max_age_seconds`).
- `CountryCriterion(include, exclude, basis, unknown, max_age_seconds)` + `.countries` (тот же кортеж, что сегодня принимает `matches_selection`), `.as_dict()`, `.from_dict()`, `.digest()`.
- `evaluate(criterion, endpoint, exit, now) -> CountryVerdict(matched, verified, reason, basis, compared, unknown, needs_measurement, conflict)`.
- `plan_measurement(...) -> MeasurementPlan(verdict, drop, judge_required, suggest_job, reason)`.
- `filter_rows(rows, criterion, resolver, now) -> FilterResult(kept, dropped, reasons, unknown, verified, total, criterion_digest)`.

**ASN, hosting, CIDR, версии**
- `ProviderFact.of(org, asn, …)`, `.claims()` → `{'hosting': bool|None, 'hosting_basis': 'org_name_heuristic'|'none', 'residential': None, 'mobile': None, 'verified': False, 'note': …}`.
- `ProviderIndex(rows|from_file)` — те же строки `(first, last, asn, organization)`, что отдаёт парсер `geoip`, плюс `cidr` покрывшей сети; `range_cidr(first, last)`.
- `database_status(path, kind, now, database, max_age)`, `read_meta`, `write_meta`, `meta_path`, `install_database(path, body, kind, version, validate)`, `geo_status(data, now, country_db, asn_db)`.

**Прочее**
- `endpoint_host`, `classify_host`, `endpoint_ip_version` (4/6/None) и `TargetCapabilities.from_samples(samples)` — две независимые оси, никогда не сливаются.
- `quota_status(basis, desired, endpoints=, exits=, now=, max_age_seconds=)` + `ExitObservation` — интерфейс данных для F14; хранение и пополнение в `pools.py`.
- `country_list()`, `search_countries(query, lang)`, `region_codes(region)`, `popular_codes()` — 64 кода с теми же RU/EN именами, что в пикере.

---

## 3. Совместимость

- **Ничего не ломается при отказе.** `geo.py` никем не импортируется; новые тесты — отдельные файлы. Существующие `tests/test_geo_want.py` (11 тестов) и `tests/test_providers.py` (3 теста) проходят после моих правок без изменений — прогон приведён в §4.
- **Строка picker не меняется.** `parse_criterion('de,NL').countries == ('DE', 'NL')` — ровно то, что возвращает `geoip.parse_countries`, и `tests/test_geo_compat.py` сравнивает моё решение с `proxytool.matches_selection` на одинаковых строках. Значение по умолчанию `unknown='exclude'` — это сегодняшнее поведение «DE,NL», оно лежит в критерии и показывается, а не подразумевается.
- **Что меняется в поведении при подключении** (нужно решить интегратору, а не мне):
  - `basis=exit` по умолчанию не включается — чтобы сегодняшний `--country` не стал фильтром по выходу. Старый флаг остаётся `endpoint`, новый `--country-basis` его расширяет.
  - В строке появятся поля `hosting_basis`, `ip_version`, `cidr` и четыре поля происхождения. `ranked.csv` получит новые колонки в конце; потребители, читающие по позиции, сломаются — это требует отметки в CHANGELOG (владелец `desktop.py`).
  - `update-geoip` начнёт писать `<database>.meta.json`. Старые базы без sidecar показываются как `stale` с `version=None` — это осознанно: «версия неизвестна» ≠ «база свежая». Через один `update-geoip` всё станет известно.

---

## 4. Проверки

Запущены в этой сессии, из корня репозитория:

```
.venv/bin/python -m unittest tests.test_geo_criterion   → Ran 26 tests … OK
.venv/bin/python -m unittest tests.test_geo_resolver    → Ran 26 tests … OK
.venv/bin/python -m unittest tests.test_geo_databases   → Ran 11 tests … OK
.venv/bin/python -m unittest tests.test_geo_filter      → Ran 17 tests … OK
.venv/bin/python -m unittest tests.test_geo_picker_parity → Ran 6 tests … OK
.venv/bin/python -m unittest tests.test_geo_compat      → Ran 4 tests … OK
.venv/bin/python -m unittest tests.test_geo_schema      → Ran 5 tests … OK (skipped=1)
```

Последний прогон — со схемой: он зовёт `db.migrate()` на временной БД, пишет endpoint через `db.endpoint_id()` и те колонки, которые заполняет мой `Resolver`, и читает их обратно. Единственный пропуск — `test_the_requested_columns_are_reported_until_the_owner_adds_them`, и он пропускает с текстом `not in the schema yet, requested in HANDOFF geo.md §1.3: endpoints.hosting_basis, observations.exit_ip, observations.exit_country, observations.exit_country_source, observations.exit_country_at, pools.quota_basis`. То есть три колонки из §1.3 действительно отсутствуют в текущем `db.py` — это не гипотеза, а вывод прогона.

Регрессия по чужому коду, который обязан остаться рабочим (это не моя проверка и не мой тест, запущено, чтобы убедиться, что picker не сломан):

```
.venv/bin/python -m unittest tests.test_geo_want → Ran 11 tests … OK
.venv/bin/python -m unittest tests.test_providers → Ran 3 tests … OK
```

Что эти тесты доказывают: пункт приёмки F08 про «один критерий страны» — одним digest'ом и одним и тем же набором строк (`test_geo_filter.OneCriterionEverywhereTests`); «read-only фильтр сам не запускает сеть» — `socket.socket`, `socket.create_connection` и `socket.getaddrinfo` подменены на исключение, `test_geo_filter.test_rows_are_not_modified_and_nothing_is_started` и `test_a_filter_without_a_resolver_confirms_nothing`; «неизвестное не выдаётся за проверенное» — `verified=False` во всех трёх политиках unknown и `quota_status(..., exit_ips=…)` с `confirmable=False`, пока есть неизвестный выход; «Endpoint DE под NL» — `test_geo_criterion.EndpointVersusExitTests`; «hostname geo по реально использованному IP и времени» — `test_geo_resolver.test_a_hostname_without_a_recorded_resolution_is_unknown` и `…_a_hostname_uses_the_address_that_was_really_used_and_when`; «hosting — эвристика» — `test_geo_resolver.test_hosting_is_a_heuristic_and_residential_stays_unmeasured`; «повреждённое обновление не стирает базу» — `test_geo_databases.test_a_damaged_update_keeps_the_last_working_database`.

Не прочитано и не выполнено: полный `unittest discover -s tests` (по условию задачи его гоняет интегратор), `tests/test_parity` (ещё не существует), `packaging/smoke.py`. Реальной загрузки GeoIP не выполнялось — все базы в тестах синтетические, файлы во временном каталоге. `db.py` прочитан только в части DDL миграций 1, 4 и 7, которые мне нужны; остальное в нём не ревьюировал.

---

## 5. Открытые вопросы

1. **Колонки §1.3.** `hosting_basis` и четыре поля наблюдаемого выхода — мой запрос владельцу `db.py`. Без `hosting_basis` F08 «hosting — эвристика, а не доказательство» нечем выразить в данных. Если `db.py` уже выпущен, прошу новую миграцию, а не правку выполненной.
2. **Основание квоты.** `quota_basis` в колонке или в `pools.policy_json` — решение владельца `pools.py`; имя `endpoints|exit_ips` предлагаю зафиксировать в обоих случаях.
3. **Политика unknown по умолчанию.** Я оставил `exclude`, потому что это сегодняшнее поведение пикера и оно ничего не утверждает. Если продуктово правильнее `require_measurement` как default — это правка одной константы `DEFAULT_UNKNOWN_POLICY` и одного теста; менять без решения владельца не буду.
4. **Список стран в двух местах.** `geo.COUNTRIES` дублирует `COUNTRIES_LIST` из `ui/app.js`. Дублирование защищено тестом `tests/test_geo_picker_parity.py`, но правильное решение — отдавать список с бэкенда (`/api/countries`) и убрать литерал из `app.js`. За `ui/*` отвечает поверхность `web`, поэтому прошу: либо оставить как есть (тест тогда остаётся обязательным), либо передать список через handoff.
5. **Сканирование без БД-IP.** `proxytool.py:2376-2379` печатает предупреждение и продолжает. Мой `Resolver` при `country_db=None` честно отдаёт unknown для всех адресов, и критерий с `unknown='exclude'` отсечёт всё. Поведение «нет базы — нет стран» стоит показать в интерфейсе как пустое состояние с причиной, а не как «ничего не нашлось».
