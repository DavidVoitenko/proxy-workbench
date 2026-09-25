# Handoff: core

**Требования:** F09 (admission, freshness, объяснение допуска), R01 (TTL сквозной путь), дефекты 1, 2, 3, 4 (см. CONTRACTS.ru.md §7.2)
**Контракт:** CONTRACTS.ru.md §2 целиком, §4.3, §4.4, §5.4, §5.5 (версия 1)
**База:** ветка `integration/ultra-2026-09-25`, ревизия `47229d0`

## 1. Прошу внести в чужие файлы

### 1.1 `proxy_workbench/proxytool.py`

- `scan.store()` (`proxytool.py:1380-1391`): вызвать `core.apply_measurement(previous, measurement, policy, access=..., collection_id=..., profile=(profile_id, profile_revision))` вместо `INSERT OR REPLACE` с payload, собранным вручную. Причина: срок `valid_until` обязан писаться один раз, при измерении (`CONTRACTS` §2.1), и не пересчитываться при экспорте с другим `--watch` (дефект 1, R01). Нынешний `stamp_freshness` (`proxytool.py:1089-1105`) подставляет `published_at` вместо неизвестного `checked_at` — этого делать больше нельзя.
- `scan.done` (`proxytool.py:1335-1340`): очередь решает `core.admit(...)`, а не `row_fresh`. Причина: `row_fresh` считает отсутствие `valid_until` вечной свежестью (`proxytool.py:1110-1112`) и не знает про `access_revision` (F04).
- `export()` (`proxytool.py:1651-1932`): выборку строк отдаёт `core.select(...)`; `status.json` дополняется `Selection.as_status()` — поля `max_age_seconds`, `expires_at`, `state_detail`, `admission_counts`. `min()` по строкам (`proxytool.py:1867`) не использовать: это дефект 3.
- `row_fresh`, `stamp_freshness`, `freshness_seconds`, `next_history`, `row_history` (`proxytool.py:1064-1116`) остаются для обратной совместимости чтения, но новый код их не вызывает. Удалять после переезда потребителей.

### 1.2 `proxy_workbench/api.py`

- `public_row` (`api.py:51-89`): поля `age_seconds`, `admission_reason`, `max_age_seconds`, `ttl_backfilled` берутся из `core.Admission.as_dict()`; `stale` больше не вычисляется через `row_fresh`.
- `select(rows, query)` (`api.py:229-246`): фильтры запроса превращаются в `core.Policy`, выборка — в `core.select`. Причина: сегодня API и GUI выбирают разными правилами, а «один scope — один состав» (F18/F29) требует одного вызова.
- `Exports.load` (`api.py:93-195`): проверять `schema_version` поколения и привязываться к имени поколения. Готовые помощники: `core.Generation`, `core.pin_generation(...)` (`E_STATE_NO_SNAPSHOT`, `E_STATE_SNAPSHOT_SCHEMA`).

### 1.3 `proxy_workbench/gui.py` и `proxy_workbench/ui/*` (поверхность `web`)

- `App.results()` (`gui.py:620-761`): выборка должна вызывать `core.select`, иначе GUI показывает другой состав, чем API (CONTRACTS §2.3, проверка `tests.test_parity`).
- Колонка возраста: значение уже готовое (`age_seconds`), пересчитывать на поверхности не нужно.

### 1.4 `proxy_workbench/gateway.py` (поверхность `gateway`)

- `Pool.refresh` (`gateway.py:109-117`): пул отбирается `core.select` по зафиксированной генерации; новая публикация не должна переключать пул (дефект 8).

### 1.5 Владелец `tests/fixtures/admission.py` (`db.py`)

- Фикстура ещё не появилась (`ls tests/fixtures` — каталога нет). Мои тесты строят строки локально, конструкторы — `row()` в каждом файле. Прошу в фикстуру добавить общий конструктор строки и общий список `(canonical, admission_reason)`, включая unknown-время, истёкшую строку и blocked; тогда тесты `core` переключаются на неё точечной правкой импорта.

### 1.6 Владелец `CONTRACTS.ru.md` (интегратор): три значения, которых нет в каноне §5.4

| Значение | Где | Зачем |
| --- | --- | --- |
| `E_TIME_TTL_MISSING` | `core.TIME_REASONS` | Новая строка без записанного срока: §2.4 требует, чтобы это не было вечной свежестью, но отдельного кода в таблице §5.4 нет |
| `E_STATE_SNAPSHOT_STATIC_TTL` | `core.STATIC_TTL_NOTICE` | F09: для static TXT TTL не enforced — это видно пользователю текстом, а не молчаливо |
| `state_detail='all_untrusted'` | `core.DETAIL_ALL_UNTRUSTED` | Отличие «всё истекло» от «время нельзя доверять» (unknown/future/rollback). Список §4.3 расширен на одно значение |

Формат всех трёх — `E_<DOMAIN>_<REASON>`, домен и причина в верхнем регистре, как требует §5.4.

## 2. Что уже сделано у меня

Публичный API `proxy_workbench/core.py` (leaf: только stdlib, без БД, DDL, сети и импортов чужих модулей):

```python
admit(row, scope, access, policy, now, *, published_at=None, clock=ClockState()) -> Admission
select(rows, scope, access, policy, now, *, published_at=None, generation=None,
       static=False, engine=None) -> Selection
apply_measurement(previous, measurement, policy, *, access=None,
                  collection_id=None, profile=None) -> dict
admission helpers: observation_state, time_state_of, capabilities_of, row_protocol, row_country
history_of(previous, ok, checked_at) -> dict
pin_generation(available, name, supported_schema_versions) -> Generation
types: Scope, Access, Policy, ClockState, Admission, Selection, Generation, Measurement
AdmissionError(code, message) — отказ входных данных или снимка, код из §5.4
AdmissionEngine(policy, state=None) — помнит часы каждого endpoint; .admit/.select/.state_dict
```

Ключевые свойства, на которые опираются потребители:

- `checked_at` — единственный источник возраста; `published_at` только отдаётся потребителю и в арифметику срока не входит.
- `valid_until` пишется один раз, в `apply_measurement`, как `checked_at + policy.max_age_seconds`; чтение под другой политикой его не двигает.
- Порядок проверок — часть контракта: нет измерения → идентичность → часы → время → исключения/качество → capabilities. `reason_code` воспроизводим.
- Явные состояния времени: `time_ok`, `time_unknown`, `time_future`, `clock_rollback`, `time_ttl_missing`, `time_expired`. Ни одно не превращается в фиктивный fresh.
- Откат часов помнится (`AdmissionEngine`) и держится, пока нет более нового измерения; состояние переживает перезапуск через `state_dict()`.
- Смешанный возраст фильтруется построчно; `Selection.expires_at` = `max()` по допущенным, а не `min()` (дефект 3). Состояния набора: `complete` / `partial` / `stale` / `empty` с `state_detail`.
- `Selection.parity_pairs` и `content_digest` дают точное определение «одинаковый состав» из §2.3 для `tests/test_parity.py`.
- `max_age_seconds` — параметр политики, видимый в `Admission.max_age_seconds` и `Selection.as_status()['max_age_seconds']`. `DEFAULT_MAX_AGE_SECONDS = 900.0` объявлен стартовым значением, а не доказанным оптимумом; `LEGACY_TTL_SECONDS = 7200` используется только для одноразового бэкфилла legacy-строки (§2.4) и помечается `ttl_backfilled=True`.

Три расширения `Scope` относительно §1.2: `network_id` (смена сети влияет на подбор так же, как смена доступа) и `Policy.allow_missing_identity` (временный переключатель на период миграции; по умолчанию строка без `access_id`/`collection_id` не допускается — fail-closed). `Policy.protocol_of/country_of/hosting_of/deny_match` — внедряемые резолверы, потому что нормализация прокси и denylist принадлежат не `core`.

## 3. Совместимость

- **Что ломается, если не внести.** `store()` без `apply_measurement` продолжит писать строки без `valid_until`; такие строки получают бэкфилл 7200 с и `ttl_backfilled=True` и остаются вечными по смыслу. Пока это так, дефект 1 и R01 закрыты не полностью: статус модуля — `implemented_unverified` в моей части и `todo` в части сканера.
- **Что не ломается.** Ни один существующий файл не изменён; `row_fresh`/`stamp_freshness`/`next_history`/`row_history` работают как раньше; схема БД не затронута; тесты `tests/test_freshness.py`, `tests/test_selection.py` и остальные не менялись и не запускались в этой сессии.
- **Миграция.** Перевод `proxytool` на `core` выполняется поштучно: сначала `store()` (запись срока), затем `done`, затем `export`/`api`/`gui`/`gateway`. Пока перевод не начат, обе реализации соседствуют и не делят состояние.

## 4. Проверки

Выполнены в этой сессии, из корня репозитория:

```
.venv/bin/python -m unittest tests.test_core_admission     → Ran 34 tests … OK
.venv/bin/python -m unittest tests.test_core_selection     → Ran 24 tests … OK
.venv/bin/python -m unittest tests.test_core_clock         → Ran 10 tests … OK
.venv/bin/python -m unittest tests.test_core_observations  → Ran 18 tests … OK
```

Что покрыто: движение времени вперёд и назад, unknown/future/rollback, mixed-age построчно, re-publish и неизменный состав, `expires_at` ≠ `min()`, смена сети/access revision/denylist как одинаковое влияние, история и последний завершённый результат, свежий fail поверх старого success, max-age как видимый параметр, статический TXT.

Что **не** прочитано и не запускалось: полный `unittest discover -s tests` (по условию задачи его гоняют другие), `tests/test_freshness.py`, `tests/test_selection.py` — я их читал, но не запускал и не правил.

## 5. Открытые вопросы

1. `access_revision` появляется в `secrets.py` + `db.py` (миграции 3 и 13). До этого `E_CONFLICT_ACCESS_REVISION` всегда закрывает пути, а приёмка F09 «новая версия доступа одинаково влияет на GUI/API/gateway/new connection» невыполнима end-to-end. В `core` контракт уже выражен; отсутствует сущность.
2. `tests/fixtures/admission.py` ещё нет (см. §1.5); переключение тестов на неё — за мной, как только файл появится.
3. `MIN_FRESHNESS_SECONDS` (`proxytool.py:420`) и `LEGACY_TTL_SECONDS` в `core` — одно и то же число в двух местах. Прошу интегратора оставить одно место; сейчас это сделано так, чтобы удаление константы из `proxytool` не сломало `core` до интеграции.
4. Приоритет времени над исходом измерения в `admit` (сначала `E_TIME_TTL_EXPIRED`, потом `E_STATE_MEASUREMENT_FAILED`) — моё решение, задокументированное в порядке проверок. Если владелец контракта предпочитает обратный порядок, меняется одна ветка и один тест.
