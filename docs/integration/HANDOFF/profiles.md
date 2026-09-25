# Handoff: profiles

**Требования:** F05 (профили и правила целей). Попутно закрывает часть приёмки
F05 «предыдущая ревизия и её результаты читаются» и совместимость с
существующими hash-конфигами.
**Контракт:** `CONTRACTS.ru.md` §1.1 (Profile), §1.2 (профиль и коллекция независимы),
§3.3 миграция 9, §5.4 (коды ошибок), §5.5 (единицы), §7.1 строка F05.
**База:** ветка `integration/ultra-2026-09-25`, `db.py` уже в дереве (волна 1),
`SCHEMA_VERSION = 14`, миграция 9 добавляет `name/revision/parent_id/digest/created_at/archived_at/is_default`.

## 1. Прошу внести в чужие файлы

### 1.1 `proxy_workbench/proxytool.py` — три конкретных места

1. **`open_db` (`:552-571`)** — это единственное место, которому вообще разрешено
   писать DDL. Прошу заменить тело на делегирование мигратору:

   ```python
   def open_db(path):
       from . import db
       return db.open_db(path)[0]        # либо свой вызов db.migrate(path) + db.connect(path)
   ```

   Причина: `db.py` владеет `user_version`/`application_id`/бэкапом, и модули уже
   пишут через `db.connect()`. Контракт §3.2/§3.4 требует отказа на чужой
   `application_id` **до** любой записи, а сегодняшний `open_db` этого не делает.

2. **`scan` (`:1300`) — `INSERT OR IGNORE INTO profiles VALUES (?, ?)`** и
   `export` (`:1654`, `:1675`), `gui.py:416/672/830` — все они адресуют строку
   профиля по-старому, как content-addressed `id`. **Это ломает скан и экспорт
   на мигрированной базе, и это надо решить до миграции 9 в рабочем `data/`.**
   Проверено здесь на временной БД (`.venv/bin/python`, скрипт: `db.migrate(path)`
   → `proxytool.open_db(path)` → обе операции):

   ```
   migrated to 14 columns: ['id', 'config', 'name', 'revision', 'parent_id',
                            'digest', 'created_at', 'archived_at', 'is_default']
   FAIL INSERT OR IGNORE INTO profiles VALUES (?, ?) -> OperationalError
        table profiles has 9 columns but 2 values were supplied
   OK   SELECT config FROM profiles WHERE id=? -> 0 rows
   rows after both attempts: 0
   ```

   Записи не происходит — это тот же механизм, что §3.5 (1) описывает как
   намеренную защиту от старого бинарника; бьёт он здесь по текущему коду.
   (`tests/test_db_write_guard.py` проверяет `results`/`candidates`/`candidate_seen`,
   `profiles` в нём нет.)

   Правильный путь после интеграции: не писать в `profiles` из scan/export вообще.
   Content-addressed конфиг — это то, чем профиль был раньше; теперь его место
   занимает именованная ревизия (см. §2). Пока `scan` пишет в `profiles`, а
   `profiles.py` читает из неё, обе стороны должны говорить на одном ключе.

3. **`allowed_failures` (`:1129-1137`) и `check_proxy` (`:1138-1170`)** — источник
   fail-fast. Прошу заменить вычисление «сколько отказов допустимо» на вызов
   `profiles.RunState`:

   ```python
   state = profiles.RunState(spec)
   ...после каждой пробы...
       state.record(target_id, ok=1 if sample['ok'] else 0, latency_ms=sample['ms'])
       decision = state.stop(elapsed_s=time.monotonic() - started)
       if decision.stop:
           # остальные цели записываются как skipped, из state.pending_targets()
   ```

   Причина: сегодня допуск один на все цели из глобального `min_success`, и он не
   знает про `all/any/K` и про обязательный набор. Контракт F05: «fail-fast
   согласован с правилом». Точность совпадает с текущей: живой считается цель, у
   которой достижимая доля `(ok + left) / (done + left)` ещё не ниже её
   `min_success` (проверено в `tests/test_profiles_evaluate.py::FailFastTests`).

### 1.2 `proxy_workbench/gui.py` и `proxy_workbench/proxytool.py::main` — точка входа

Прошу **не** собирать правило профиля заново ни в одной из трёх поверхностей.
Единая точка:

```python
verdict = profiles.run_request({'profile_id': pid, 'profile_revision': rev,
                                'evidence': items}, store=profiles.ProfileStore(db))
```

`evidence` — список `TargetEvidence` (или словарей с теми же полями), по одному
на цель; остальное модуль разбирает сам. Для отладочного показа профиля без сети
есть `profiles.plan(spec)` — «что проверит и что потребует», тот же dict, что
отдаётся в `verdict['profile']`.

### 1.3 `proxy_workbench/i18n.py` (владелец — поверхность `web`)

Прошу добавить переводы для кодов, которые возвращает модуль. Коды машинные,
тексты — только в `i18n`; в самом модуле русских текстов для них нет намеренно.
Список — `profiles.REASON_CODES` (ключ → английское пояснение) плюс коды ошибок:
`E_CONFLICT_NAME`, `E_CONFLICT_ARCHIVED`, `E_STATE_PROFILE_UNKNOWN`,
`E_SECRET_IN_PROFILE`. Остальное (`E_VALIDATION_*`, `E_CONFLICT_REVISION`,
`E_IMPORT_FORMAT`, `E_IMPORT_REVISION`, `E_DATA_DB_FOREIGN`, `E_LIMIT_BUDGET`)
уже есть в §5.4.

### 1.4 `docs/integration/CONTRACTS.ru.md` §5.4 — предлагаю три новых кода

| Код | Почему нужен | Куда в §5.4 |
| --- | --- | --- |
| `E_CONFLICT_NAME` | Два живых профиля с одним именем — конфликт, а не ошибка поля; `E_CONFLICT_REVISION` про ревизии, `E_VALIDATION_FIELD` про форму | домен `CONFLICT`, 409 |
| `E_CONFLICT_ARCHIVED` | Операция над профилем в архиве: это состояние, а не «не найден» | домен `CONFLICT`, 409 |
| `E_STATE_PROFILE_UNKNOWN` | Неизвестный `profile_id`/`profile_revision`; `E_STATE_NO_SNAPSHOT` означает другое (нет опубликованного снимка) | домен `STATE`, 404/409 |

Без них API пришлось бы отдавать `E_VALIDATION_FIELD` на несуществующий профиль,
а это вводит в заблуждение и отличается от правила §5.3 «для запрещённого объекта
возвращается тот же код, что для отсутствующего».

### 1.5 `proxy_workbench/proxytool.py` — перенос существующих hash-конфигов

Уже есть готовый мост `profiles.from_legacy_config(config)`: content-addressed
конфиг превращается в спецификацию ревизии 1, где все цели обязательные и
глобальный `fail_fast.min_success` копируется в порог каждой цели. Правило
приёмки не меняется. Прошу вызвать его при переносе строк
`name IS NULL` в именованные профили. Имя для таких строк не выдумывается
(требование F02, дефект 11): переносить их должен пользователь явно, либо они
остаются в таблице без имени и не видны в библиотеке.

## 2. Что уже сделано у меня

Публичный API `proxy_workbench/profiles.py` (1484 строки, `__all__` в начале):

**Конфигурация (чистая, без ввода-вывода).**

| Вызов | Что делает |
| --- | --- |
| `ProfileSpec.create(targets=…, optional_rule=…, k=…, budget=…, attempts=…, description=…)` | строит и полностью проверяет спецификацию; `targets` — список словарей или `TargetRule` |
| `ProfileSpec.from_dict(payload, validate=False)` | разбор документа; `validate=False` — для чужой/старой строки: не падает, но и не может дать pass |
| `spec.to_dict() / to_json() / digest / plan()` | документ, стабильный content-hash, «что проверит и что потребует» |
| `TargetRule`, `OptionalRule`, `Budget`, `TargetEvidence`, `TargetAssessment` | узкие типы; неизвестное поле → `E_VALIDATION_UNKNOWN_FIELD`, а не игнор (F07) |

**Решение — единственное для всех трёх поверхностей.**

| Вызов | Что делает |
| --- | --- |
| `evaluate(spec, evidence) -> dict` | чистая функция: pass/fail, причина, разбивка по целям, число успешных проб |
| `profiles.plan(spec) -> dict` | тот же «dry-run», без измерений |
| `RunState(spec)` → `.record(target_id, ok=…, latency_ms=…, unknown=…)`, `.stop(elapsed_s=…)`, `.can_pass()`, `.pending_targets()`, `.evaluate()` | накопление проб одного запуска и решение fail-fast |
| `target_fingerprint(target)` | отпечаток порогов цели; его несёт каждое доказательство |
| `run_request(payload, store=…) -> dict` | одна точка входа GUI/CLI/API |

**Хранилище.** `open_store(path | data_dir)` → `db.migrate()` + `db.connect()`;
`ProfileStore(conn)` — без DDL и без починки схемы. Методы: `create`, `copy`,
`update(profile_id, spec, base_revision=…)`, `archive`, `unarchive`, `set_default`,
`get`, `head`, `history`, `find`, `list`, `default`, `free_name`, `diff`.
`ProfileRef(profile_id, revision)` — то, что передают наружу; `record.row_id`
это `<profile_id>@<revision>`.

**Обмен.** `export_profile(store, id, revision=None, history=False)` и
`import_profile(store, payload, name=None, on_conflict='fail'|'rename'|'reuse')`;
`assert_secret_free(payload)`; `from_legacy_config(config)`.

Ключевые решения, которые стоит проверить при приёмке:

1. **Строка = ревизия.** Таблица объявляет `id` единственным первичным ключом,
   поэтому ключ строки — `<profile_id>@<revision>`, `name` группирует ревизии,
   `parent_id` их связывает (у копии `parent_id` указывает на исходную ревизию).
   Схему менять не нужно: миграции 9 хватает.
2. **Доказательство несёт свои пороги.** `TargetEvidence.fingerprint` — отпечаток
   `min_success`/`max_latency_ms` цели. Evidence с чужим отпечатком получает
   состояние `unmeasured` и причину `E_TARGET_STALE_EVIDENCE`: понижение порога
   не может купить pass под измерения, которых при этом пороге не делали (F05).
   Роль цели (`required`/`optional`) в отпечаток **не** входит — она меняет
   aggregation, а не годность измерения.
3. **Fail-fast разрешён только когда pass недостижим.** `RunState.stop()` отвечает
   на один вопрос: может ли любое завершение оставшихся проб дать pass. Раньше
   допуск считался один на все цели; теперь он per-target и учитывает
   `all`/`any`/`K`. Обязательные цели всегда обязательны, optional-набор живёт по
   своему правилу (`all`/`any`/`at_least(K)`/`none`), и пустой optional-набор
   допустим только при явной политике `none`.
4. **Непустое подтверждение — на трёх уровнях.** `validate()` отказывается создать
   профиль, который не может дать pass (пустой набор, `K<=0`, все цели выключены,
   `K` больше optional-набора, `budget.max_probes` меньше `spec.min_probes`); если
   документ всё же пришёл извне, `evaluate()` даёт `pass=False` с конкретной
   причиной; и любой verdict требует хотя бы одной успешной измеренной пробы.
5. **Обновление ничего не затирает.** `update` дописывает ревизию; неизменное
   содержание ревизии не добавляет; `base_revision` даёт `E_CONFLICT_REVISION`;
   `export_profile(history=True)` + `import_profile` восстанавливают цепочку целиком.
6. **Секретов в документе нет по построению.** Экспорт собирается из типизированных
   полей, `assert_secret_free` — вторая линия для рукописного конфига.

## 3. Совместимость

- **Что ломается, если не внести §1.1:** после `db.migrate()` на рабочей базе
  позиционные вставки в `profiles` (`proxytool.py:1300`) перестают работать, то
  есть скан и экспорт падают. Это ровно тот отказ, который §3.5 (1) считает
  намеренной защитой от старого бинарника, но в текущем коде он бьёт по новому.
- **Что не ломается:** существующие таблицы и данные, `results`, `api.Exports`,
  gateway, тесты других модулей. Модуль не меняет ни одну существующую таблицу и
  не пишет DDL; существующие (legacy) строки профилей остаются как есть, с
  `name IS NULL`, и не появляются в библиотеке.
- **`jobs.py`/`core.py`:** `core.Scope` уже несёт `profile_id` и
  `profile_revision` — это те же два поля, что у `ProfileRef`; `core.Policy`
  (`min_success`, `max_latency_ms`) сейчас общий для всех целей, а профиль даёт
  их per-target. Стыковка описана в §1.1 п.3 и требует решения владельца `core.py`:
  либо `Policy` выводится из ревизии профиля, либо admission продолжает работать
  по общему порогу, а per-target решается только в профиле. **Это открытый вопрос,
  не решённый здесь.**

## 4. Проверки

Команды, выполненные в этой сессии (Python 3.14, `.venv`), все — зелёные:

```
$ .venv/bin/python -m unittest tests.test_profiles_spec
Ran 27 tests in 0.001s — OK

$ .venv/bin/python -m unittest tests.test_profiles_evaluate
Ran 30 tests in 0.015s — OK

$ .venv/bin/python -m unittest tests.test_profiles_store
Ran 24 tests in 0.168s — OK

$ .venv/bin/python -m unittest tests.test_profiles_parity
Ran 18 tests in 0.074s — OK

$ .venv/bin/python -m unittest discover -s tests -p 'test_profiles_*.py'
Ran 99 tests in 0.247s — OK
```

Что покрыто и чем доказано:

- **Таблица истинности** `mandatory + all/any/K/none` — `RequiredPlus*Tests`,
  `OptionalNoneTests` (все четыре режима, включая «K не считает обязательные»).
- **`K>size`, `K<=0`, `any(empty)`, zero-effective-probe, все выключенные** —
  `NonEmptyGuaranteeTests`: каждый отказ на создании и каждый не-pass на решении.
- **`unknown` и `skipped` не проходят** — `UnknownSkippedUnmeasuredTests`.
- **Смена порога не даёт pass старым измерениям** — `ThresholdChangeTests` плюс
  `RevisionTests.test_the_previous_revision_still_decides_on_its_own_evidence`
  (та же история через `run_request` по `profile_revision` 1 и 2).
- **Fail-fast согласован с правилом** — `FailFastTests`: остановка только при
  недостижимом pass, обязательные цели, `at_least`, бюджет проб и времени,
  cut-цели помечаются `skipped`.
- **Порядок с плавающей точкой совпадает с существующим admission-путём** —
  `test_the_float_rule_is_the_one_the_admission_path_already_uses` сверяет с
  `reputation.result_allowed` на 2/3, 0.7, 0.5.
- **Одинаковый вердикт в GUI/CLI/API** — `ParityTests`: пять представлений одного
  профиля (объект, словарь формы, `argparse.Namespace`, JSON туда-обратно, вызов по
  `profile_id`) дают побайтово одинаковый JSON.
- **Import/export без секретов** — `InterchangeTests`: canary-строка не появляется
  ни в документе, ни в строках БД; документ с `password` или адресом с
  userinfo отвергается; `reuse` идемпотентен; история восстанавливается цепочкой.
- **Своей DDL нет** — `NoDdlTests.test_the_store_issues_no_ddl_of_its_own` трассирует
  все операторы `CREATE/ALTER/DROP/PRAGMA` за цикл create/update/copy/default/
  archive/unarchive и требует пустой список; соседний тест требует отказа
  `E_DATA_DB_FOREIGN` на базе без контрактных колонок.
- **Совместимость с legacy-базой** — `LegacyDatabaseTests`: база, созданная
  `proxytool.open_db`, мигрируется на месте, её строка профиля сохраняется
  (`digest` = прежний content hash, `name IS NULL`), именованный профиль
  добавляется рядом, повторный `db.migrate()` не меняет ни байта.

Что **не** прогонялось здесь: полный `unittest discover -s tests` (по условию
задачи его гоняют другие исполнители и интегратор) и `packaging/smoke.py`.

## 5. Открытые вопросы

1. **Ключ строки профиля.** `<profile_id>@<revision>` — моё решение из
   ограничения «`id` объявлен единственным первичным ключом». Если интегратор
   предпочтёт отдельную таблицу ревизий (`profiles` + `profile_revisions`), API
   не меняется: `ProfileRef` уже несёт оба поля, а `revision_key`/`parse_row_id`
   — единственное место, которое знает про формат ключа.
2. **`is_default` держится на ревизиях.** `set_default` ставит флаг на все строки
   профиля, чтобы «профиль по умолчанию» не зависел от того, какая ревизия читается.
   Если нужен другой смысл (например, флаг только у head), это правка одной
   строки в `_clear_default`/`set_default`.
3. **Общий `codes.py`.** `core.py` и `profiles.py` независимо определяют
   `E_VALIDATION_FIELD`, `E_CONFLICT_REVISION` и `REASON_CODES`. Для i18n нужен
   один реестр кодов; владельца у него нет, поэтому я его не создавал.
4. **Перенос legacy-профилей в библиотеку.** `from_legacy_config` готов, но
   автоматического переноса нет: имя нельзя выдумать (F02). Нужно решение
   владельца `importer.py`/`gui.py` о том, как пользователь подтверждает имена.
5. **Жёсткая зависимость от `db.py`.** `profiles.py` импортирует `db` на уровне
   модуля и не имеет DDL-запасного пути. Это соответствует HANDOFF §2.1/§2.2
   (`db.py` — первая волна, он обязателен), но означает, что изолировать или
   удалить `db.py` нельзя без пересмотра этого модуля. Если понадобится обратная
   независимость, путь не в DDL внутри `profiles.py`, а в другом: `db.py`
   импортирует декларацию модуля, как это уже сделано в `jobs.py`.
