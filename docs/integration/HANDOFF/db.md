# Handoff: db

**Требования:** F02 (коллекции и scope, дефект 11), F24 (данные, backup, restore, retention, защита от записи старой схемой)
**Контракт:** `docs/integration/CONTRACTS.ru.md` §3.1–§3.6 (версия 1), §2.3 (общая фикстура), §5.1 (секреты), §5.4 (коды ошибок)
**База:** ветка `integration/ultra-2026-09-25`, ревизия `269a011` на момент написания

## 1. Прошу внести в чужие файлы

### 1.1 `proxytool.py:552-571` — `open_db` (владелец: интегратор)

Заменить тело на вызов мигратора; больше этот файл не создаёт схему:

```python
def open_db(path):
    conn, report = db.open_db(path, app_version=VERSION)
    return conn
```

`db.open_db` сам делает `migrate()`, потом `connect()` и `assert_current()`, и
возвращает пару `(connection, MigrationReport)`. Старый контракт «функция
возвращает только соединение» сохраняется строкой выше; вызывающему коду, которому
нужен отчёт о миграции (CLI, GUI при первом запуске), доступна полная пара.

Почему: `CONTRACTS §3.2` — «единственная точка DDL», и §3.5.4 — проверка версии на
чтении и на записи до любого `CREATE`/`ALTER`/`INSERT`.

### 1.2 `proxytool.py:648-667` (`collect.add`) и `:1380-1391` (`store`) — интегратор

- `INSERT OR IGNORE INTO candidates VALUES (?)` → `INSERT OR IGNORE INTO candidates(proxy, endpoint_id) VALUES (?, ?)`,
  где `endpoint_id = db.upsert_endpoint(db, normalize(value))`.
- `INSERT OR IGNORE INTO candidate_seen VALUES (?, ?)` → явный список из трёх колонок.
- `store()` → `INSERT INTO results(...)` с полным списком колонок, и **все шесть**
  колонок ключа заполнены: `profile_id`, `profile_revision`, `access_id`,
  `access_revision`, `endpoint_id`, `job_id`. Пустые значения допустимы только как
  `''`/`0` (соглашение миграции 13), иначе доказательства разных ревизий доступа
  схлопнутся обратно в одну строку.
- Чтение кандидатов для scan — с `collection_id` в scope; membership берётся из
  `membership`, а не из `candidates` (см. 1.3).

### 1.3 `importer.py`, `probes.py`, `exportsvc.py`, `pools.py`, `jobs.py` (волна 2)

- В своих тестах вызывать `db.migrate(path)`, а не объявлять DDL.
- Импорт в личную коллекцию: `db.create_collection(...)` создаёт **пустую** коллекцию;
  старые публичные адреса в неё не попадают. Это и есть дефект 11.
- Признак «свой» не выдумывается: `origin` в `membership` — это provenance, а не
  доверие к адресу.

### 1.4 `maintenance.py:72-87` — `clear_runtime` (владелец: `desktop.py`)

Дублирующая реализация удаления не нужна: у `db.py` есть `cleanup(data, apply=False)`
(-preview с размерами) и `cleanup(data, apply=True)`. `RUNTIME_FILES`/`RUNTIME_DIRS`
`db.cleanup` импортирует, а не копирует, — список остаётся один. `PRESERVED_FILES`
(`gui-settings.json`, `denylist.txt`) и `PRESERVED_SECRET_FILES` исключены из
удаления жёстко, переопределить их параметром нельзя.

### 1.5 `paths.py` + F23 (владелец: `desktop.py`)

Перенос папки данных: `db.migrate_data_path(old, new, apply=False)` — превью,
`apply=True` — копия с backup старой БД в `new/backups/`. Старая папка не удаляется:
удаление остаётся отдельным явным действием пользователя.

### 1.6 `secrets.py`: vault в `data/`

Если vault окажется внутри `data/`, его надо добавить в `RUNTIME_FILES` явной
строкой (владелец файла — `desktop.py`) **или** оставить его в
`db.PRESERVED_SECRET_FILES`. Сейчас `db.cleanup` не трогает `secrets.json`,
`secrets.vault`, `credentials.json`, `vault` и показывает их в превью как
`secret material`.

## 2. Что уже сделано у меня

`proxy_workbench/db.py` — единственный писатель DDL в пакете. Публичный API:

| Область | Вызов | Что возвращает |
| --- | --- | --- |
| Версия | `db.SCHEMA_VERSION` (14), `db.APPLICATION_ID` (0x50574231) | константы |
| Открытие | `db.migrate(path, *, backup_dir=None, app_version=None, now=None, create=True)` | `MigrationReport(status, previous_version, applied, backup, legacy_candidates)` |
| Открытие | `db.open_db(path, **kwargs)` | `(sqlite3.Connection, MigrationReport)` — замена `open_db` |
| Проверка | `db.probe(path)`, `db.current_version(path)`, `db.assert_current(conn)` | `(user_version, application_id)` / `int` |
| Соединение | `db.connect(path, *, read_only=False)` | соединение с `foreign_keys=ON`, WAL, без неявных транзакций |
| Интроспекция | `db.describe/tables/columns/indexes/primary_key/table_bytes/database_bytes` | словари и списки |
| Endpoint | `db.endpoint_id(canonical)`, `db.upsert_endpoint(conn, canonical, **fields)` | `str` id |
| Коллекции | `create_collection`, `rename_collection`, `archive_collection`, `get_collection`, `list_collections`, `add_member`, `remove_member`, `collection_members`, `endpoint_collections`, `legacy_summary` | id / dict / list |
| Backup | `create_backup(source, backup_dir, *, reason, name, now)`, `verify_backup`, `list_backups`, `manifest_path` | `BackupManifest` |
| Restore | `restore_preview`, `restore`, `rollback`, `migrate_data_path` | `RestorePreview` / `RestoreReport` |
| Retention | `retention_preview`, `apply_retention` | `RetentionPreview` / `RetentionReport` |
| Очистка | `cleanup_preview`, `cleanup` | `CleanupPreview` / `CleanupReport` |
| Секреты | `secret_bindings`, `rebind_secrets` | `SecretBinding` / `RebindReport` |

Миграции `0..14` — исполняемые функции в `db.MIGRATIONS`, порядок и состав как в
`CONTRACTS §3.3`. Каждая идемпотентна, каждая применяется в одной транзакции вместе
с записью `PRAGMA user_version` и строки в `schema_migrations`.

Общая фикстура `tests/fixtures/admission.py` (назначена `db.py` в §2.3 CONTRACTS и
§2.1 HANDOFF): 7 случаев, фиксированное `NOW`, готовые `EXPECTED_ADMITTED`,
`EXPECTED_REJECTED`, `EXPECTED_ALL` (точные отсортированные пары
`(canonical, reason_code)`), `FIXTURE_DIGEST` и `insert_rows(conn)` для записи в
мигрированную БД. Импорт: `from tests.fixtures.admission import ...`.

## 3. Совместимость

- **Что ломается, если не внести 1.1–1.3.** После миграции 5 позиционные вставки
  старого воркера в `results`, `candidates`, `candidate_seen` падают с
  `OperationalError` — это намеренный механизм F24/§3.5.2, а не регресс. Новый код
  обязан писать явными списками колонок. `candidate_meta` оставлен совместимым
  намеренно (§3.5.2).
- **Что не ломается.** Существующие тесты репозитория не тронуты: `proxytool.open_db`
  и его схема в дереве не менялись, `db.py` ничего не импортирует из
  `proxytool.py` и не создаёт вторую БД. `db.py` — новый leaf-модуль, его
  подключение к движку делает интегратор.
- **Данные.** Legacy-база (`user_version = 0`) открывается, получает pre-migration
  backup, мигрируется 0→14, её строки сохраняются; кандидаты переезжают в
  `endpoints` и в коллекцию `legacy-collected` с `origin='legacy'`, публичная база
  остаётся пустой. Повторный `migrate()` на текущей базе не меняет ни байта.
- **Отказ = без записи.** `user_version > 14`, чужой `application_id` и не-SQLite
  файл отклоняются до первой записи; проверено сравнением sha256 файла.

## 4. Проверки

Команда (из корня репозитория), каждый файл отдельно — все зелёные:

```
.venv/bin/python -m unittest tests.test_db_migrations      Ran 20 tests ... OK
.venv/bin/python -m unittest tests.test_db_write_guard     Ran  6 tests ... OK
.venv/bin/python -m unittest tests.test_db_collections     Ran 12 tests ... OK
.venv/bin/python -m unittest tests.test_db_backup          Ran 16 tests ... OK
.venv/bin/python -m unittest tests.test_db_retention       Ran 14 tests ... OK
.venv/bin/python -m unittest tests.test_db_secrets        Ran 12 tests ... OK
.venv/bin/python -m unittest tests.test_db_fixtures       Ran  9 tests ... OK
```

Прогон с предупреждениями о ресурсах (каждый файл, `-W always::ResourceWarning`):
предупреждений `ResourceWarning` нет. Полный `unittest discover -s tests` не
запускался — по условию задачи его выполняет интегратор; прогон
`-p "test_db_*.py"` (только мои файлы, 80 тестов) — OK.

## 5. Открытые вопросы

1. **Коды причин: `E_TIME_*` или §2.4 без префикса.** §2.4 пишет
   `reason_code=TIME_UNKNOWN`, §5.4 объявляет каноном `E_TIME_UNKNOWN`. Фикстура
   использует `E_*` (§5.4 — «единый канон»), а `REASON_ALIASES` переводит
   написание §2.4. Решение нужно подтвердить у владельца `core.py`; если канон —
   §2.4, меняется одна таблица в фикстуре.
2. **Два литерала без кода.** `admitted` (для включённой строки) и `blocked`
   (отказ политикой до измерения) в §5.4 не описаны. Взяты как есть и помечены в
   фикстуре; если нужен `E_*`-код — это правка контракта, а не фикстуры.
3. **Индекс `observations`.** §3.3 просит `observations(endpoint_id, checked_at DESC)`,
   но таблица из миграции 4 не имеет `checked_at` (есть `started_at`/`finished_at`),
   а §3.2 разрешает индексы только по существующим колонкам. Сделано
   `observations(endpoint_id, started_at DESC)`. Если правильным считается
   `finished_at` — это правка одной строки миграции 14.
4. **`endpoints_canonical` дублирует UNIQUE.** `endpoints.canonical` уже UNIQUE, то
   есть индекс есть. Строка из §3.3 реализована как написано; интегратор может
   её убрать как мёртвый вес.
5. **Миграция 0 создаёт пять пре-версионных таблиц.** В §3.3 этого нет, но без них
   свежая база, созданная версионированным кодом, не получила бы `candidates`,
   `results` и `profiles`, которые читает движок. Все пять — с `IF NOT EXISTS`, на
   legacy-базе они уже есть и не меняются.
6. **Backup занимает место в `data/backups/`.** `maintenance.RUNTIME_FILES` этот
   каталог не перечисляет, поэтому `clear_runtime` его не удалит, а
   `db.cleanup_preview` его и не покажет. Нужно решение владельца `desktop.py`:
   входит ли `backups/` в runtime-очистку (моё мнение — нет: backup пользователю
   дороже, чем runtime-хвост).
