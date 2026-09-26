# Handoff: area-ddl (схема: таблицы источников, пауза и бюджет расписаний, исключения области)

**Требования:** F13, F15, F27, F02, CONTRACTS §1.1, §3.2, §3.3, §3.5
**База:** `integration/ultra-2026-09-25`
**Мои файлы:** `proxy_workbench/db.py`, `tests/test_area_ddl_{sourcedesk,migration,runtime}.py`, этот файл
**Чужие файлы не менялись.** Всё, что нужно в другом файле, — в §6.

---

## 0. Коротко

| Пункт | Состояние | Где доказано |
| --- | --- | --- |
| Семь таблиц источников в живой базе | **сделано** (миграция 16) | `tests/test_area_ddl_sourcedesk.py`, §4.1 |
| `schedules.paused/pause_reason/resume_at/counters_json/last_run_at` | **сделано** (миграция 17) | `tests/test_area_ddl_runtime.py`, §4.3 |
| `candidate_scope_exclusion` | **сделано** (миграция 18) | `tests/test_area_ddl_runtime.py`, §4.4 |
| Идемпотентность, одна транзакция, версия 15→18 | **сделано** | `tests/test_area_ddl_migration.py` |
| Резервная копия перед неаддитивной миграцией, включая `user_version` 1..12 | **сработала** | §4.2 |
| Старый бинарник не пишет в новую схему | **сработало** (отказ по версии) | §4.5 |
| Миграция большой базы по памяти | **измерено**: 35.7 МБ база → пик 2.2 МБ | §4.6 |

`SCHEMA_VERSION` 15 → **18**. Три новые миграции, все аддитивные.

---

## 1. Таблицы источников — миграция 16

### 1.1 Во что превращается `REQUESTED_DDL`

Задание предлагало поискать вызов, который формирует финальные DDL «возможно через `_dialect`
или подстановку схемы». **Такого вызова нет.** Проверено:

```
$ grep -rn "_dialect" --include="*.py" . | grep -v .venv | grep -v ./build/
(пусто)
$ grep -rn "REQUESTED_DDL" --include="*.py" .
proxy_workbench/sourcedesk.py:1406:  REQUESTED_DDL = (          # объявление
tests/test_sourcedesk_store.py:43:  for statement in sd.REQUESTED_DDL:   # единственное исполнение
tests/test_sourcedesk_compare.py:393: for statement in sd.REQUESTED_DDL:
tests/test_areasources_feeds.py:…
```

`sourcedesk.REQUESTED_DDL` — кортеж из **четырёх** строк: две таблицы (`source_feed`,
`membership_source`) и два индекса. Подстановки параметров нет, `_dialect` нет, схемы-префикса
нет. Единственное место, где кортеж исполняется, — тесты, которые строят свою схему в памяти.
`db.py` объявляет те же четыре объекта у себя; `tests/test_area_ddl_sourcedesk.py::test_the_declared_tables_match_the_requested_ddl_exactly`
сравнивает их через `PRAGMA table_info`, так что расхождение ломает тест, а не продакшн.

Семь таблиц из задания — это четыре объекта из `REQUESTED_DDL` плюс DDL поколений из ветки
`sources-catalog` (отдельное дерево, HEAD `5263b55`, `proxytool.py:528-578`). Ветку не менял,
читал.

### 1.2 Одно переименование при переносе

Ветка адресует строки колонкой `proxy` (`source_generation_entry(generation_id, proxy)`).
CONTRACTS §1.1 делает `endpoints(id, canonical)` единственной сущностью адреса, и это же
требование зафиксировано в `HANDOFF/sources-handoff.ru.md` §1.2 п. 6. Поэтому колонка названа
`endpoint_id` — ровно то, чего требует приёмка этой ветки (§3.1 п. 3), и ровно то, что
`HANDOFF/sources-handoff.ru.md` §2 C7 называет конфликтом.

### 1.3 Внешние ключи — и почему не везде

Объявленный DDL **не содержит ни одного внешнего ключа**. Я объявил два, и только там, где
родитель всегда пишется раньше ребёнка:

```sql
source_generation.observation_id      INTEGER REFERENCES source_observation(id)
source_generation_entry.generation_id  INTEGER NOT NULL REFERENCES source_generation(id)
```

`membership_source` остался **без** внешних ключей, и это решение закреплено тестом
`test_membership_source_has_no_foreign_key_to_endpoints`. Причина — факт о модуле, а не о вкусе:
`SourceDesk.apply_plan` кладёт в `endpoint_id` канонический адрес, который пришёл из
`ImportedEndpoint.endpoint` (это строка `http://host:port`, а не `endpoints.id`):

```python
# sourcedesk.py:1203
endpoints = tuple(dict.fromkeys(getattr(entry, 'endpoint', entry) for entry in result.entries))
# -> plan.added -> SourceDesk.add_membership -> INSERT INTO membership_source(endpoint_id, …)
```

Ссылка на `endpoints(id)` отвергла бы собственную запись модуля. Схема не должна ломать
работающий DML — это работа `sourcedesk.py` (конфликт C7), не моя.

### 1.4 Индексы

Два объявленных (`membership_source_by_source`, `source_feed_by_collection`) плюс пять под
реальные чтения, которые иначе сканируют таблицу целиком при каждом вызове:

| Индекс | Кто читает |
| --- | --- |
| `source_observation_by_source(source_id, id DESC)` | `source_management.history()`, `runtime_snapshot()` |
| `source_generation_by_source(source_id, id DESC)` | `source_management.cache_state()` |
| `source_generation_by_observation(observation_id)` | внешний ключ поколения |
| `source_state_by_source(source_id)` | `runtime_snapshot()` |
| `membership_source_by_endpoint(collection_id, endpoint_id)` | `SourceDesk.contributors()` |

Тест проверяет не наличие имени, а план запроса: `EXPLAIN QUERY PLAN` обязан выбрать индекс.

---

## 2. Колонки паузы и бюджета — миграция 17

`HANDOFF/scheduler.md` §1.1 просил добавить их **в миграцию 7**, потому что «таблицы ещё нигде не
созданы». Это было верно, когда писалось. Сейчас `SCHEMA_VERSION = 15`, базы в 15 существуют, и
правка `_m7` их бы не коснулась: `migrate()` применяет миграции с `version >= user_version`, а у
базы 15 миграция 7 давно позади. **Поэтому — новая аддитивная миграция 17**, а не правка 7.

Имена взяты из `scheduler.REQUESTED_COLUMNS` — модуль уже содержит свой список, и тест
`test_the_columns_the_module_asks_for_are_the_columns_that_exist` сверяет его с `PRAGMA
table_info` по всем тринадцати именам, а не по моему перечислению:

```
schedules:     last_run_at, paused, pause_reason, resume_at, dst_policy, catch_up,
               max_catch_up, wake_gap_s, power_json, notify_json, notifications_json,
               counters_json, updated_at
schedule_run:  reason, missed
```

Типы — из §1.1 того же документа. `paused`/`catch_up`/`missed` — `INTEGER NOT NULL DEFAULT 0`,
`max_catch_up` — `NOT NULL DEFAULT 1`. Значения по умолчанию честные: строка, записанная до
миграции, читается как «не на паузе, бюджет не потрачен», а не как «пауза с нулевым счётчиком».

---

## 3. Исключения области — миграция 18

Маршруты GUI, которые я поддержал, и что они делают:

| Маршрут | Метод | Операция | Колонки, которые ему нужны |
| --- | --- | --- | --- |
| `/api/sources/exclude-scope` | `App.exclude_source_scope` (`gui.py:858`) | исключить адреса, которые отдал один источник; по умолчанию только эксклюзивные, `include_shared` снимает фильтр | `proxy`, `as_seen`, `source_id`, `reason`, `created_at`, флаг «разделяемый» |
| `/api/sources/scope` | `App.scope_exclusions` (`gui.py:851`) | показать, что исключено сейчас, и кем | все те же + `scope_digest` |
| `/api/sources/scope/clear` | `App.clear_scope_exclusions` (`gui.py:921`) | снять все или по источнику | `source_id`, `scope_digest` |

```sql
CREATE TABLE candidate_scope_exclusion(
    proxy TEXT NOT NULL, reason TEXT, source_id TEXT,
    scope_digest TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL, expires_at REAL,
    as_seen TEXT,
    exclusive INTEGER NOT NULL DEFAULT 1,
    shared INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(proxy, scope_digest),
    CHECK(exclusive <> shared)) WITHOUT ROWID
```

Первые шесть колонок и ключ совпадают с `candidate_scope_exclusion` ветки `sources-catalog`
(`proxytool.py:580`), последние три — то, чего у неё нет и что нужно маршрутам. Имена
`shared`/`exclusive` — один факт под двумя именами: GUI хранит `shared` и рассуждает об
«эксклюзивных адресах», читатель любого имени должен получать правду, поэтому `CHECK` делает
противоречивую строку несохранимой (`IntegrityError` — проверено в §4.4).

Добавлено четыре функции хранения рядом с `add_member`/`remove_member` (§1.2 модуля): это
примитивы хранилища, без них «исключение убирает адрес из результатов» нечем проверить.

```python
db.add_scope_exclusion(conn, proxy, *, source_id=None, scope_digest=db.DEFAULT_SCOPE,
                       as_seen=None, reason="source_scope", shared=False,
                       expires_at=None, now=None)
db.scope_exclusions(conn, *, scope_digest=..., source_id=None, now=None, include_expired=True)
db.excluded_addresses(conn, *, scope_digest=..., now=None, include_expired=True) -> frozenset
db.clear_scope_exclusions(conn, *, source_id=None, scope_digest=...) -> int
```

Ни одна из них не трогает `candidates`, `endpoints`, `membership` или `membership_source` —
исключение это утверждение об области, а не об адресе, поэтому отмена это `DELETE`
(source-system-design §12.3: «данные не удаляются»).

---

## 4. Фактический вывод проверок

Все прогоны — на временных базах, `python3` 3.14 / SQLite 3.53.1. Скрипты и их вывод:
`/tmp/area-ddl-verify/`.

### 4.1 Пустая база

```
db.SCHEMA_VERSION = 18  migrations = [0..18]

[1] db.migrate() on an empty path
    status=created previous_version=0 -> schema_version=18
    applied=[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18]
    backup=None
    total tables: 31 (was 23)
    present: ['source_state', 'source_observation', 'source_generation',
              'source_generation_entry', 'source_identity', 'source_feed',
              'membership_source', 'candidate_scope_exclusion']
    absent : []
    schedules cols added: ['last_run_at', 'paused', 'pause_reason', 'resume_at', 'counters_json']
    new indexes: ['candidate_scope_exclusion_by_scope', 'candidate_scope_exclusion_by_source',
                  'membership_source_by_endpoint', 'membership_source_by_source',
                  'source_feed_by_collection', 'source_generation_by_observation',
                  'source_generation_by_source', 'source_observation_by_source',
                  'source_state_by_source']

[2] db.migrate() again on the same file
    status=current applied=() changed=False backup=None
    file bytes identical: True
    user_version=18 application_id=0x50574231
    third call: status=current applied=()  -> idempotent=True
```

`file bytes identical: True` — побайтово, не «схема та же».

### 4.2 База, написанная предыдущей сборкой, и все промежуточные версии

```
[3] a database the previous build (v15) wrote, with real rows
    before: tables=23  source_feed present=False  schedules.paused present=False
    before: endpoints=5 membership=5 schedules=1
    migrate -> status=migrated previous_version=15 applied=[15, 16, 17, 18]
    backup: {'schema_version': 15, 'reason': 'pre-migration',
             'sha256': '42b971dc…7193f', 'tool': 'VACUUM INTO', 'bytes': 229376, …}
    after : tables=31  source_feed=True  schedules.paused=True  candidate_scope_exclusion=True
    after : endpoints=5 membership=5 schedules=1 (rows preserved)
    legacy schedule row: paused=0 counters_json=None enabled=1

[4] every intermediate version 0..17 opens and reaches 18
    user_version  0 -> 18  applied=[0..18]              backup=yes  ok=True
    user_version  1 -> 18  applied=[1..18]              backup=yes  ok=True
    user_version  2 -> 18  applied=[2..18]              backup=yes  ok=True
    user_version  3 -> 18  applied=[3..18]              backup=yes  ok=True
    user_version  4 -> 18  applied=[4..18]              backup=yes  ok=True
    user_version  5 -> 18  applied=[5..18]              backup=yes  ok=True
    user_version  6 -> 18  applied=[6..18]              backup=yes  ok=True
    user_version  7 -> 18  applied=[7..18]              backup=yes  ok=True
    user_version  8 -> 18  applied=[8..18]              backup=yes  ok=True
    user_version  9 -> 18  applied=[9..18]              backup=yes  ok=True
    user_version 10 -> 18  applied=[10..18]             backup=yes  ok=True
    user_version 11 -> 18  applied=[11..18]             backup=yes  ok=True
    user_version 12 -> 18  applied=[12, 13, 14, 15, 16, 17, 18]  backup=yes  ok=True
    user_version 13 -> 18  applied=[13, 14, 15, 16, 17, 18]      backup=yes  ok=True
    user_version 14 -> 18  applied=[14, 15, 16, 17, 18]          backup=yes  ok=True
    user_version 15 -> 18  applied=[15, 16, 17, 18]              backup=yes  ok=True
    user_version 16 -> 18  applied=[16, 17, 18]                  backup=no   ok=True
    user_version 17 -> 18  applied=[17, 18]                      backup=no   ok=True
    ALL VERSIONS OK = True
```

Про названный в задании баг: **версии 1..12 обновляются, и копия берётся.** Логика
`pending & DESTRUCTIVE_MIGRATIONS` считает миграции с `version >= user_version`, поэтому
`13` и `15` попадают в `pending` для любого файла старше 15. Для 16 и 17 копии нет — там
только аддитивное, копировать нечего.

### 4.3 `SourceDesk` на миграционной базе

```
[5] sourcedesk.SourceDesk against the migrated database
    bind()          -> source_id=ref-e0c44b8a60538676 mode=replace
    public_url      -> https://sub.invalid/v1/list   (token stripped: True)
    membership(src)  = ('…100.7:8080', '…100.8:8080', '…100.9:8080')
    membership(other)= ('…100.9:8080',)
    contributors(SHARED) = ('ref-418b2a…', 'ref-e0c44b8a…')   <- many-to-many
    replace refresh -> removed=(…100.8, …100.9) retained_shared=(…100.9,)
    membership(src) after  = ('…100.7:8080',)
    membership(other) after= ('…100.9:8080',)   <- чужое membership не тронуто
    last_good = ('…100.7:8080',) expires_at = 21700.0
    failure -> membership=(…100.7) last_good=(…100.7) failures=1 next_attempt_at=True
    304 -> membership=(…100.7) expires_at unchanged=True
    canary leak check: none
```

Подписка (F27), поколение, last-good, backoff, «сбой не стирает рабочую коллекцию», «304 — только
валидация транспорта» и many-to-many происхождение работают на живой базе.

### 4.4 Пауза и бюджет через перезапуск

```
[6] schedules: pause and daily budget across a restart
    persistence(): {'persists_runtime_state': True, 'persists_counters': True,
                    'lost_on_restart': []}
    ДО  restart: paused=1 pause_reason=paused_user counters_requests=3/3 counters_bytes=3072
    --- перезапуск: все хендлы закрыты, файл открыт заново ---
    ПОСЛЕ restart: paused=True pause_reason=paused_user counters_requests=3/3 counters_bytes=3072
    last_run_at preserved: True
    engine sees paused=True
    tick(paused) -> run_requests=0 decisions=[('skip', 'paused_user')]
    ledger after restart: reserve ok=False  denial=E_LIMIT_BUDGET/requests_exhausted remaining=0.0
```

Было `ДО: paused=True, counters=3/3` → `ПОСЛЕ: paused=False, counters_requests=0`. Стало
`3/3 → 3/3`, и исчерпанный дневной бюджет после перезапуска **отказывает** резервированию,
а не отдаёт лимит обратно. `scheduler.persistence()['lost_on_restart']` теперь `[]`.

### 4.5 Старый бинарник

`assert_current` отказывает по версии, и это уже встроенный механизм, а не мой новый:
`SCHEMA_VERSION`-сборка, знающая меньше миграций, получает `E_DATA_DB_VERSION_AHEAD`; сборка,
знающая больше, — `E_DATA_MIGRATION_FAILED` с подсказкой `migrate()`. Оба отказа проверены на
реальном файле 18 (`OldBinaryRefusalTests`). Записи до отказа не происходит: `open_db` не
возвращает соединение, `user_version` файла остаётся 18.

Новые миграции **не** добавляют «защитную колонку» в духе `WRITE_GUARD_COLUMNS`: старый
бинарник до этих таблиц не дотянется — его отсекает заголовок файла.

### 4.6 Память на большой базе

Синтетическая база: 200 000 адресов в `endpoints`, файл **35.66 МБ**.

```
db size=35.66 MB   migrate peak=2.23 MB   elapsed=0.07 s   applied=[15, 16, 17, 18]
```

Пик — 6 % размера файла и не растёт с ним: миграции 16..18 только `CREATE … IF NOT EXISTS` и
`ALTER TABLE ADD COLUMN`, ни одной строки существующей таблицы не читают. Проверено
`tracemalloc` в тесте, порог — `size // 8`. Для сравнения, резервная копия той же базы:
`VACUUM INTO` + `sha256_file` мегабайтными кусками, пик 2.23 МБ на файле 8.28 МБ.

---

## 5. Риски моих миграций

1. **`migrate()` переприменяет миграцию, равную `user_version`.** Цикл берёт `version >= user_version`,
   поэтому база 16 при обновлении снова выполняет 16. Все миграции идемпотентны, так что это
   безвредно, но **новая миграция обязана быть идемпотентной** — иначе она выполнится минимум
   дважды. Закреплено тестом `test_a_migration_at_the_files_own_version_runs_again_and_must_be_a_no_op`.
2. **`membership_source` без внешнего ключа на `endpoints`.** Осознанно (§1.3), но пока
   `apply_plan` пишет туда канонический адрес, в базе лежат две модели адреса рядом
   (`membership.endpoint_id` — дайджест, `membership_source.endpoint_id` — строка). Это конфликт
   C7 из `HANDOFF/sources-handoff.ru.md` §2 и он мой миграцией **не решён** — только не усугублён.
3. **Схема выросла на 23 страницы.** Измерено: минимум пустой базы 56 страниц (v15) → 79 (v18),
   то есть ~92 КБ на файле с 4 КБ страницей. На маленьких базах заметно, на рабочих — нет.
   Именно этим ломается `test_db_retention.py::test_vacuum_is_opt_in` (§6.1).
4. **`source_generation_entry.generation_id` — внешний ключ.** Кто-то, кто напишет записи поколения
   раньше самого поколения, получит `IntegrityError`. Порядок «сначала поколение, потом записи»
   заложен в саму форму таблицы, так что риск маленький, но он есть.
5. **`exclusive` и `shared` — CHECK, а не вычисляемая колонка.** Пишет кто-то одну из двух —
   вторая должна быть выставлена вручную; забыл — `IntegrityError`. Сознательный обмен
   простоты на явную ошибку вместо молчаливого расхождения.
6. **Индексы на пустых таблицах.** Девять новых индексов на семи пустых таблицах — это
   9 страниц по 4 КБ, которые тратятся сразу. Приемлемо; для базы, где сбор не ведётся,
   это заметный, хотя и маленький, оверхед.

---

## 6. Что осталось за чужими файлами

Пять мест. Четыре — упавшие тесты, которые я не могу править, пятое — настоящий дефект,
который моя схема сделала достижимым.

### 6.1 `tests/test_db_migrations.py` — список таблиц (упавший тест)

`CONTRACT_TABLES` (строка 14) сравнивается точным равенством, а в базе теперь 31 таблица.

```python
# tests/test_db_migrations.py:14
CONTRACT_TABLES = (..., "export_artifact", "import_batch",
                   # добавить:
                   "source_state", "source_observation", "source_generation",
                   "source_generation_entry", "source_identity", "source_feed",
                   "membership_source", "candidate_scope_exclusion")
```

### 6.2 `proxy_workbench/desktop.py` — зеркало версии (упавший тест)

```
FAILED tests/test_desktop_signing.py::SchemaMirrorTests::
       test_the_fallback_schema_version_matches_the_storage_layer
E   AssertionError: 15 != 18
```

`desktop.py:61 FALLBACK_SCHEMA_VERSION = 15` → **18**. Номер только в этом файле: при
импортерованном `db` берётся `db.SCHEMA_VERSION`, константа нужна для случая «db не
импортировался». Комментарий над ней это уже признаёт.

### 6.3 `tests/test_areajobs_scheduler.py` — тест, который фиксировал пробел (упавший тест)

```
FAILED …::test_a_pause_and_a_spent_budget_are_reported_as_lost_when_the_schema_cannot_hold_them
E   AssertionError: [] != ['counters', 'paused']
```

Тест называл дефект своим же именем («an honest report of a gap the migrator has not closed
yet») и утверждал `assertFalse(after.state('nightly').paused, 'this is the defect being
reported')`. Миграция 17 закрыла дефект, поэтому тест **устарел, а не сломан**. Заменяется
проверкой `persistence()['lost_on_restart'] == []` и кругом «пауза и потраченный бюджет
переживают перезапуск»; такой круг уже есть в
`tests/test_area_ddl_runtime.py::PauseAndBudgetRoundTripTests::test_a_pause_and_its_spent_budget_survive_a_restart`.

### 6.4 `tests/test_sourcedesk_compare.py` — схема, построенная поверх мигратора (упавший тест)

```
FAILED …::FetchStateTests::test_a_feed_that_delivered_addresses_is_not_delivered_nothing
E   sqlite3.OperationalError: table source_feed already exists   (tests/test_sourcedesk_compare.py:393-394)
```

`FetchStateTests.setUp` (строки 392-394) делает `db.open_db(...)` — а это уже создаёт `source_feed` —
и **затем** исполняет `sd.REQUESTED_DDL` поверх. Пока миграции не было, это работало. Сейчас
`REQUESTED_DDL` нужно либо убрать (`db.open_db` даёт ту же схему), либо пропускать уже
существующие таблицы. Второе — менее честно: тест перестаёт проверять, что мигратор и модуль
согласны.

### 6.5 `proxy_workbench/scheduler.py` — настоящий дефект, который моя схема сделала достижимым

**Это важнее четырёх падений выше.** `SqliteScheduleStore.save_spec` пишет спецификацию так:

```python
# scheduler.py:1803 -> 1830
self.connection.execute(
    f'INSERT OR REPLACE INTO {name} ({", ".join(columns)}) VALUES ({placeholders})', values)
```

`INSERT OR REPLACE` в SQLite — это `DELETE` + `INSERT`. Колонки, которых нет в списке,
получают значение по умолчанию. В списке `save_spec` нет `paused`, `pause_reason`, `resume_at`,
`counters_json` и `last_run_at`. Живой замер:

```
after save_state : (1, '{"requests": 3, …}')
after save_spec  : (0, None, 120.0)      <- interval изменён 60 -> 120
```

То есть пауза и дневной счётчик **переживают перезапуск процесса, но молча обнуляются, когда
пользователь правит интервал расписания**. До моей миграции обнулять было нечего: колонок не
существовало. Теперь — есть, и `INSERT OR REPLACE` их затирает.

Чинится в `scheduler.py`, не в `db.py`: заменить `INSERT OR REPLACE` на
`INSERT … ON CONFLICT(id) DO UPDATE SET` по тем же колонкам (тогда неименованные сохранятся),
либо после `save_spec` перечитывать состояние и записывать его заново. Схемой это не лечится
осознанно: триггер, «спасающий» значения, спрятал бы привычку писать, а не исправил её.
До правки `scheduler.py` F15 закрыт наполовину: **перезапуск** починен, **правка расписания** — нет.

### 6.6 Найдено попутно, не мой файл

`proxy_workbench/apikeys.py:190 ensure_schema()` исполняет `executescript` с `CREATE TABLE` для
миграций 8, 10 и индекса 14 — в обход мигратора. Идемпотентно, поэтому ничего не ломает, но
нарушает `HANDOFF/sources-handoff.ru.md` §3.1 п. 2. Мой тест
`test_nothing_outside_db_py_executes_a_create_statement` держит это как именованное исключение
`KNOWN_SCHEMA_WRITERS`, чтобы список не мог вырасти молча. Своё я ничего не менял.

---

## 7. Быстрый чек-лист приёмки

```bash
python3 -m pytest tests/test_area_ddl_sourcedesk.py tests/test_area_ddl_migration.py \
                 tests/test_area_ddl_runtime.py -q
# 54 passed, 61 subtests passed

python3 -c "
from proxy_workbench import db
import tempfile, pathlib
d = pathlib.Path(tempfile.mkdtemp())/'data'/db.DB_FILENAME
db.migrate(d)
c = db.connect(d)
need = ['source_state','source_observation','source_generation','source_generation_entry',
        'source_identity','source_feed','membership_source','candidate_scope_exclusion']
print('absent:', [t for t in need if t not in db.tables(c)])
print('schedules:', [x for x in ('paused','pause_reason','resume_at','counters_json','last_run_at')
                     if x in db.columns(c,'schedules')])
"
# absent: []
# schedules: ['paused', 'pause_reason', 'resume_at', 'counters_json', 'last_run_at']
```

Пять пунктов §6 закрываются по одному файлу. 6.5 — единственный, где после правки
F15 можно считать закрытым полностью.
