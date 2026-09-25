# Handoff: jobs

**Требования:** F11 (задания и восстановление), дефект 6, дефект 25 (совместно с поверхностью `web`), R17 (совместно с `web`).
**Контракт:** CONTRACTS.ru.md §6 (состояния job и item), §3.3 миграция 6, §5.4 (коды), §5.6 (события), §6.4 (один writer), §4.3 (`state_detail`). Версия контракта 1.
**База:** ветка `integration/ultra-2026-09-25`. `db.py` к концу работы уже в дереве: его `_m6`
(`proxy_workbench/db.py:715-731`) создаёт ровно те четыре таблицы, что и `jobs.SCHEMA`, и мои
тесты идут через `db.migrate()` (HANDOFF §2.2), а не через приватную копию схемы.

## 1. Прошу внести в чужие файлы

### 1.1 `proxy_workbench/db.py` — владелец мигратора

- **Миграция 6 — общий источник DDL.** Сейчас `_m6` (`proxy_workbench/db.py:715-731`) и
  `jobs.SCHEMA` описывают одну схему двумя текстами. Миграция 6 в CONTRACTS §3.3 объявлена с
  совместным владельцем `db.py` + `jobs.py`, а HANDOFF §2.2 запрещает двум модулям иметь
  разные представления схемы. Прошу заменить тело `_m6` на вызов
  `jobs.install_schema(conn)` — это четыре `CREATE TABLE IF NOT EXISTS`, идемпотентно, ничего
  кроме этих таблиц не создаёт. Сейчас обе версии совпадают (проверено сравнением
  `PRAGMA table_info` в `tests/test_jobs_lifecycle.py`, тест
  `test_the_migrator_and_the_store_agree_on_migration_six`), но совпадение держится на
  соглашении, а не на коде.
- **Индекс `job(idempotency_key)`.** Его нет ни в §3.3, ни в текущем `db.py`: проверено —
  на мигрированной базе индексы `job`, `job_item`, `job_event`, `checkpoint` это только
  `sqlite_autoindex_*` плюс `job_item_state` из миграции 14. Идемпотентность сейчас держится
  на `BEGIN IMMEDIATE` плюс правило одного writer'а (§6.4); уникальный индекс сделает её
  независимой от дисциплины вызывающего. Прошу добавить в миграцию 6 или 14:
  `CREATE UNIQUE INDEX IF NOT EXISTS job_idempotency_key ON job(idempotency_key)
  WHERE idempotency_key IS NOT NULL`. Ключ уникален на весь срок жизни job, а job никогда не
  удаляется, поэтому частичный индекс не конфликтует ни с чем.
- **`PRAGMA foreign_keys=ON`** (уже требование §3.2) обязателен и для соединения мигратора:
  без него `job_item.job_id REFERENCES job(id)` не проверяется. Мои тесты включают прагму явно
  и содержат проверку, что item чужого job не проходит.

### 1.2 `proxy_workbench/proxytool.py` — интегратор, `scan` и `run_scan`

- `scan` (`proxytool.py:1283-1506`) перестаёт быть единственным носителем состояния выполнения:
  очередь и состояние items переходят в `JobStore`. Что именно просижу:
  1. перед стартом `store.submit(kind, scope, items, idempotency_key=...)`, где `scope` —
     `jobs.Scope(collection_id, profile_id, profile_revision, profile_digest, filters, budgets)`,
     а `items` — `jobs.QueueItem` на каждый кандидат коллекции;
  2. вместо `pending` из `candidates` минус `done` (`proxytool.py:1341-1348`) очередь берётся
     из `store.claim(job_id)`, который возвращает `None`, когда очередь пуста;
  3. `store.record_observation(...)` сразу после записи наблюдения и
     `store.finish_item(job_id, item_id, 'done', observation_id=...)` — по факту завершённого
     измерения, а не до него. Это и есть дефект 6 в коде: отменённый recheck сохраняет прежний
     payload (`proxytool.py:1318-1330`) именно потому, что перезапись происходит по факту измерения;
  4. `store.heartbeat(job_id, ...)` вместо/вместе с `update_progress` (`proxytool.py:2400-2403`),
     и `store.enforce_deadline(job_id)` вместо только per-request таймаутов — общий срок задания
     это §6.2, а не `attempts × len(targets)`;
  5. `update_progress`/`gui-progress.json` остаются для совместимости, но живая лента должна
     читать `store.events(job_id, after_seq=cursor)` (дефект 25: строка «Проверено 512/1000» не
     является событием измерения).
- **Прямо не прошу** переписывать `store()` или `scan` целиком: нужны точечные подстановки в
  перечисленных местах, второй worker-протокол в `jobs.py` намеренно не заведён.

### 1.3 `proxy_workbench/gui.py` и `proxy_workbench/api.py` — интегратор и `web`

- `App.start` (`gui.py:369-508`) сейчас отказывает, если job уже идёт, и пишет `gui-job.json`.
  Просится перевод на `JobStore`: `POST` с `Idempotency-Key` → `store.submit(...)`, повтор
  возвращает тот же `job_id` (F11, приёмка F29 №3) вместо ошибки 400.
- `App.stop` (`gui.py:522-527`) создаёт stop-файл; просится `store.cancel(job_id)` с
  `reason_code`, чтобы отмена была состоянием, а не файлом, и чтобы её можно было повторить
  идемпотентно.
- `App._wait` (`gui.py:513-518`) при ненулевом коде выхода вызывает `store.fail(job_id, reason_code=...)`;
  `store.recover()` вызывается при старте GUI, до чтения таблицы.
- `workbench.lock` (`proxytool.py:2323-2335`) остаётся границей writer'а. Если интегратор хочет
  тот же механизм внутри процесса, `jobs.WriterGate(os_lock=exclusive_lock)` принимает готовый
  колбэк — вторую реализацию файлового лока заводить не нужно.

### 1.4 CONTRACTS §5.4 — владелец контракта

Прошу внести в канон кодов четыре значения, которые `jobs.py` уже возвращает:

| Код | Когда |
| --- | --- |
| `E_STATE_JOB_NOT_FOUND` | `jobs.JobNotFound` — job с таким id не существует |
| `E_STATE_ITEM_NOT_FOUND` | `jobs.ItemNotFound` — item с таким id не существует в job |
| `E_STATE_JOB_TRANSITION` | `jobs.StateConflict` — состояние не допускает перехода (`pause` после `succeeded`, `done` без наблюдения) |
| `E_STATE_JOB_INTERRUPTED` | событие восстановления после аварии: heartbeat истёк, job переведён в `paused` |
| `OBSOLETE_MEMBERSHIP` | `error_code` item'а, который ушёл из коллекции после создания job; item становится `blocked`, а не «проверен» |

Первые четыре лежат в домене `STATE` рядом с существующим `E_STATE_JOB_RUNNING`; последнее —
не ошибка измерения, а вердикт политики, поэтому домен `STATE` тоже подходит, а не `UPSTREAM`.

## 2. Что уже сделано у меня

`proxy_workbench/jobs.py`, leaf-модуль без сетевого ввода и без второго нормализатора. Публичный API:

```python
jobs.install_schema(conn)                     # DDL миграции 6, идемпотентно
jobs.JobStore(conn, *, clock=time.time, max_queue_items=1_000_000)

store.submit(kind, scope, items, idempotency_key=None) -> Job     # created+queued, идемпотентно
store.create_job(kind, scope, items=(), *, idempotency_key=None, job_id=None) -> Job
store.enqueue(job_id, items) -> int
store.queue(job_id) -> Job

store.start(job_id) -> Job                    # E_CONFLICT_BUSY, если другой job running
store.pause(job_id, *, reason_code=None, idempotency_key=None) -> Job
store.resume(job_id, *, scope=None, member_ids=None, membership_epoch=None,
             idempotency_key=None) -> Job
store.cancel(job_id, *, reason_code=None, idempotency_key=None) -> Job
store.retry(job_id, *, items=None, scope=None, kind=None, idempotency_key=None) -> Job
store.finish(job_id, *, state=None, reason_code=None) -> Job
store.fail(job_id, *, reason_code, detail='') -> Job
store.enforce_deadline(job_id) -> Job         # -> timed_out по budgets.timeout_s

store.claim(job_id, *, item_id=None) -> JobItem | None
store.mark_prefiltered(job_id, item_id) -> JobItem
store.record_observation(job_id, item_id, observation_id) -> JobItem
store.finish_item(job_id, item_id, state, *, observation_id=None, error_code=None,
                  verdict=None) -> JobItem
store.items(job_id, *, state=None) -> list[JobItem]
store.item(job_id, item_id) -> JobItem
store.unfinished(job_id) -> int
store.sync_membership(job_id, member_ids, *, membership_epoch=None) -> dict

store.save_checkpoint(job_id, name, state, *, emit=True) -> dict
store.checkpoint(job_id, name=None) -> dict | None
store.drop_checkpoint(job_id, name) -> None
store.heartbeat(job_id, **fields) -> dict
store.mark_suspended(job_id) -> dict
store.mark_awake(job_id) -> dict              # добавляет сон к бюджету
store.slept_s(job_id) -> float
store.recover(*, heartbeat_timeout_s=300.0) -> Recovery

store.job(job_id) -> Job
store.jobs(*, kind=None, state=None, limit=None) -> list[Job]
store.progress(job_id) -> Progress
store.events(job_id, *, after_seq=0, limit=500, types=None) -> list[JobEvent]
store.emit(job_id, type, code, data=None, *, item_id=None) -> JobEvent
store.last_seq(job_id) -> int

jobs.WriterGate(*, os_lock=None).hold(owner, path=None)   # §6.4
```

Типы: `Scope` (замороженный, с `profile_revision` и `budgets.timeout_s`), `QueueItem`, `Job`,
`JobItem`, `JobEvent` (форма §5.6 ровно: `stream, seq, at, type, job_id, item_id, code, data`),
`Progress`, `Recovery`. Ошибки: `JobError` и подклассы с полем `.code`.

Ключевые решения, которые видны только из кода и которые стоит проверить ревьюеру:

- `Scope` — неизменяемый (`frozen` + глубокая заморозка `filters`/`budgets`), `input_digest`
  считается один раз при создании из `kind` + `scope` + точной очереди и больше не меняется.
- `resume` не принимает очередь, а только опционально `scope` **для проверки**: другое значение —
  `E_CONFLICT_REVISION`, а не тихий рескан.
- Уход из membership реализован только как `blocked` + `OBSOLETE_MEMBERSHIP`. Адрес, ушедший из
  коллекции, не возвращается в `pending`, если вернулся: `blocked` — терминальное состояние item'а.
- `writer`-гарантия двойная: `start()` отказывает, когда другой job в `running`
  (`E_CONFLICT_BUSY`), и `WriterGate` для процесса; каждая мутация идёт в `BEGIN IMMEDIATE`,
  поэтому два вызывающих не могут переплести чтение-изменение-запись.
- `done` требует `observation_id`, а запись состояния и id наблюдения идут одной транзакцией:
  отказ не оставляет item'у ни нового состояния, ни нового id.

## 3. Совместимость

- **Что ломается, если не внести §1.** Пока `scan` не переведён на `JobStore`, состояние задания
  остаётся в `gui-job.json` и в пересчитываемой `pending`-выборке: F11 не закрыт, crash recovery
  невозможен, а `E_CONFLICT_BUSY` не появится на уровне job. Конкретный сценарий отказа: краш
  воркера посреди recheck — items `probing` теряются, очередь пересчитывается заново от
  `row_fresh`, и наблюдение, записанное без вердикта, не привязано ни к какому item'у.
- **Что НЕ ломается.** `jobs.py` не меняет ни одного существующего файла, не трогает
  `candidates`, `results`, `profiles`, не удаляет ни одного job и не пишет вне своих четырёх
  таблиц (проверено трассировкой SQL, см. §4). Существующие тесты не затрагиваются: ни один
  существующий модуль не импортирует `jobs`.
- **Миграция.** Нет файлов на диске кроме существующей БД, нет `RUNTIME_FILES`, нет форматов
  экспорта. Формат `scope_json` — канонический JSON с отсортированными ключами; читается
  обратно через `Scope.from_json`.
- **Секреты.** `access_id` и `secret_ref` событиям несут, значение credentials — нет: `emit`,
  `save_checkpoint` и `finish_item` отказывают в payload с ключом-именем credential'а и в
  значении с URL userinfo. Canary-проверка в тестах ищет сгенерированный секрет в байтах файла
  БД, в WAL и во всех JSON-выгрузках.

## 4. Проверки

Команды запускались из корня репозитория, по одной на файл, только мои тесты:

```
.venv/bin/python -m unittest tests.test_jobs_lifecycle   -> Ran 27 tests, OK
.venv/bin/python -m unittest tests.test_jobs_items       -> Ran 25 tests, OK
.venv/bin/python -m unittest tests.test_jobs_recovery    -> Ran 17 tests, OK
.venv/bin/python -m unittest tests.test_jobs_events      -> Ran 15 tests, OK
```

Файлы: `tests/test_jobs_support.py` (фикстуры), `tests/test_jobs_lifecycle.py`,
`tests/test_jobs_items.py`, `tests/test_jobs_recovery.py`, `tests/test_jobs_events.py`. База в
фикстуре создаётся `db.migrate()`, то есть тесты идут по настоящей миграции 6; отдельный тест
сравнивает её `PRAGMA table_info` с тем, что создаёт `jobs.install_schema` в отдельной
in-memory базе.

Что эти проверки действительно доказывают:

- повтор запроса с тем же `Idempotency-Key` не создаёт второй job (одна строка в `job`, три в
  `job_item`, ни одного нового события), а тот же ключ с другим входом — `E_CONFLICT_IDEMPOTENCY`;
- `pause`, `cancel`, `recover` и `retry` не меняют ни одного `observation_id` и не удаляют
  ни одной строки `job_item`; `retry` создаёт новый job, а старый остаётся читаемым;
- `resume` не добавляет items при выросшей коллекции и блокирует ушедших из неё;
- checkpoint, `observation_id` незавершённого item'а и `paused`-состояние переживают переоткрытие
  файла БД; `recover()` идемпотентен и не трогает живой job;
- сон не считается аварией, а `slept_s` добавляется к дедлайну: восемь часов сна не превращают
  job в `timed_out`, а час работы — превращают;
- события идемпотентны по `seq` без пропусков, курсор `after_seq` возвращает только хвост,
  измерение — одно событие на item с заполненным `item_id`;
- ни один SQL не обращается к `results`, `observations`, `candidates`, `endpoints`, `membership`,
  `profiles`, `collections`, `accesses`, `pools`, `api_keys`; `DELETE` возможен только из `checkpoint`;
- модуль работает и на соединении в режиме `isolation_level=''`, который выдаёт текущий
  `open_db`, не меняя его настройку.

Что осталось непрочитанным: полный `unittest discover -s tests` по условию задачи запускает
интегратор после сборки всех модулей; в этой сессии он не запускался. Отдельно проверено вне
тестов, что `jobs.install_schema` на базе, созданной текущим `proxytool.open_db`, даёт девять
таблиц и полный жизненный цикл без единой строки в `results`.

## 5. Открытые вопросы

1. `job_event` не имеет колонки `item_id` (миграция 6 §3.3), а форма события §5.6 — имеет. Я
   положил `item_id` в `data_json` и поднимаю его в `JobEvent.item_id`. Если владелец контракта
   предпочитает отдельную колонку, это аддитивная миграция, а не переписывание модуля.
2. `slept_s` живёт в checkpoint `suspend`, потому что в `job` нет колонки бюджета. Если нужен
   бюджет в SQL (например, для отчёта по всем job), это ещё одна аддитивная колонка.
3. Граница «одна БД, один writer» держится на `BEGIN IMMEDIATE` плюс отказе `start()` при чужом
   `running` job. Для команды, которая пишет в ту же БД мимо `JobStore`, нужен `WriterGate` с
   `os_lock` — это вызов интегратора, не мой файл.
4. Свободный текст в `fail(detail=...)` и `verdict` проверяется на два обнаружимых вида секретов
   (ключ-имя credential'а и userinfo в URL). Секрет, вставленный в диагностическую строку
   вызывающим, хранилище не отфильтрует — это граница контракта, а не недоработка: строка должна
   приходить из `diagnostics.py`, а не из `repr` исключения.
