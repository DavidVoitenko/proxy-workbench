# Handoff: apikeys

**Требования:** F29 (менеджер ключей, права и scope, audit), дефект 18 (один секрет на API/шлюз/GUI), R12.
**Контракт:** CONTRACTS.ru.md §5.1–5.4, §3.3 (миграции 8, 10, 14) — версия 1.
**База:** ветка `integration/ultra-2026-09-25`, HEAD `47229d0` плюс мои два файла.

Модуль `proxy_workbench/apikeys.py` написан и покрыт тестами. Ниже — только то, что требуется от владельцев чужих файлов. Я ничего из перечисленного не правил.

## 1. Прошу внести в чужие файлы

### 1.1 `proxy_workbench/db.py` — владелец `db.py`

- **Миграция 8 (`api_keys`):** первые шестнадцать колонок совпадают с CONTRACTS §3.3 дословно — так и написано в `db.py:746-751`, расхождения нет. Прошу **добавить четыре**: `disabled_at REAL`, `previous_verifier TEXT`, `previous_verifier_salt TEXT`, `previous_verifier_algo TEXT`.
  - `disabled_at` — обратимое отключение ключа. В контрактной схеме есть только `revoked_at`, а `revoke` необратим, поэтому «выключить и потом включить» было бы неотличимо от отзыва.
  - `previous_verifier*` — superseded-верификатор узкого окна ротации. Трёх верфикаторных колонок на две одновременно действительные пары не хватает: без них старый секрет нельзя принять внутри окна и нельзя отозвать отдельно.
- **Миграция 8 (`audit_log`) и миграция 14 (индекс `api_keys(prefix)`)** — совпадают с §3.3 дословно, ничего менять не нужно.
- **Исполняемый текст DDL** этих двух миграций лежит в `proxy_workbench/apikeys.py:113-185` (`_API_KEYS_DDL`, `_AUDIT_LOG_DDL`, `_API_KEYS_COLUMNS_DDL`, `ensure_schema`). Он написан один раз, и это единственное представление. `ensure_schema(conn)` идемпотентна: на уже мигрированной базе ничего не меняет, отсутствующую колонку добавляет через `PRAGMA table_info` (тот же приём, что `proxytool.py:566-570`).
  - **Прошу выбрать одно из двух:** либо миграции 8/10 в `db.py` вызывают `apikeys.ensure_schema`, либо `db.py` создаёт таблицы сам, а `apikeys` остаётся только потребителем. Своё определение DDL в `db.py` при расхождении с `apikeys` приведёт к тихой несовместимости, поэтому вариант «скопировал и забыл» — худший из трёх.
  - **До этого решения `apikeys` работает и без четырёх колонок:** выдача, список, аутентификация, переименование, отзыв, удаление, ротация без окна — идут по контрактным колонкам, а `INSERT`/`UPDATE` собираются из списка колонок, отфильтрованного по `PRAGMA table_info`. `disable`, `rotate(grace_s>0)` и `purge_expired_grace()` на такой базе отказывают **с внятной ошибкой** `E_AUTH_INVALID`, где в `detail` названа недостающая колонка, а в `action` — `apikeys.ensure_schema(conn)`; сырого `OperationalError` пользователь не увидит. Проверено на реально мигрированной базе: `tests/test_apikeys_schema.py`.
- **Причина именно здесь:** CONTRACTS §3.3 объявляет `api_keys` и `audit_log` общими для `db.py` и `apikeys.py`, а `db.py` на момент начала работы отсутствовал в дереве. Модуль не мог ни выполнить миграции, ни быть проверен.

### 1.2 `proxy_workbench/api.py` и новый `proxy_workbench/apiv1.py` — интегратор и исполнитель `apiv1.py`

- **Уже есть `KeyStore` в `apiv1.py:369`** (`REQUIRED = ('verify', 'list_keys', 'create_key', 'rotate_key', 'revoke_key', 'update_key', 'audit')`) — адаптер пишете вы, он принадлежит вам, потому что он строит **ваш** `Principal` (`apiv1.py:311`). Recipe:
  - `verify(secret)`: `mgr.authenticate(secret)`; `ApiKeyError` → `None` (ваш `_authenticate` сам превращает это в `E_AUTH_INVALID`, `apiv1.py:1606`). Возврат `None` на отказ согласован с вашим контрактом. Состояние `disabled` до `apiv1` не доходит: `authenticate` его уже отклоняет, поэтому в вашем `Principal` поля `disabled_at` не нужно.
  - `Principal.key_id/permissions/collections/pools/expires_at/revoked_at/rate_limit/concurrency/last_used_at` ← из `KeyInfo` (`apikeys.py:460`). `rate_limit` у вас — кортеж `(requests, window_seconds)`, у меня `RateLimit.requests` / `.window_s`; `concurrency.max_active` → ваш `int`.
  - `kind='api_key'`; `'bootstrap'`/`'subscription'`/`'legacy'` — ваши отдельные виды, см. §5.2.
  - `list_keys/create_key/rotate_key/revoke_key/update_key` — прямые вызовы `mgr.list_keys/create/rotate/revoke/update_metadata`; `audit(record)` — `mgr._audit`. Последнее имя приватное: если нужен публичный вход, добавлю `mgr.record(...)` по вашей просьбе, а не заставляйте `apiv1` лезть в `_audit`.
- **Точка аутентификации — один вызов на запрос:** `ApiKeyManager.authenticate(secret, permission=..., collection_id=..., pool_id=..., include_secrets=...)`. Он читает revoked/disabled/expires на каждом вызове, поэтому отдельного кэша «валидных ключей» в API быть не должно.
- **Коды и статусы уже проставлены:** `apikeys.HTTP_STATUS` отображает код в HTTP (`401/403/429/400`). Телу ответа — `ApiKeyError.as_json()`: `{"error": {"code", "detail", "action", "retry_after", "state"}}`.
- **Соединение с БД открывать с `check_same_thread=False`.** `ThreadingHTTPServer` уже используется в `api.py:352`, а `sqlite3` по умолчанию привязывает соединение к создавшему потоку. Менеджер сам сериализует доступ (`threading.RLock`), но соединение из другого потока он спасти не может. Это всплыло тестом `tests/test_apikeys_quota.py` (`test_the_guard_survives_concurrent_holders`).
- **Legacy read-only токен** (`--api-token`/`PROXY_WORKBENCH_API_TOKEN`, `api.py:249-252`) не заводится через `apikeys`: у него ровно нынешние права `read.*` и он не должен получать `admin.*` при миграции. Совместимость — отдельным compatibility path по F29.
- **Секрет управляющего ключа — только в `Authorization: Bearer`**, не в query. `?token=` остаётся только для legacy-читателей — ваш `allow_query_token` это уже делает.
- **Границы объектов:** `apikeys.object_visible(principal, kind, object_id, collection_id=...)` и `apikeys.effective_collections(principal, requested)` отвечают одинаково на «чужой объект» и «нет такого объекта». Их надо вызывать у **каждой** операции: batch, job status, event stream, download, export artifact. Ваш `require_scope` отвечает `E_STATE_NOT_FOUND`/404 — это ваш выбор кода, и он не противоречит §5.3 (одинаков для чужого и отсутствующего); расходиться с моим `E_AUTH_SCOPE`/403 надо только если решите один код на оба случая.

### 1.3 `proxy_workbench/gui.py`, `proxy_workbench/ui/*`, `proxy_workbench/i18n.py` — поверхность `web`

- **Bootstrap:** `mgr.bootstrap_admin(local_trusted=True, name=...)` вызывает только локальный GUI/CLI. Флаг — обязательное keyword-без-значения: забыть его нельзя, а передать его может только код, который уже проверил свой транспорт. Сетевой обработчик его не ставит.
- **Диалог выдачи:** секрет берётся из `IssuedKey.secret` и показывается один раз; `IssuedKey.warnings` уже переведены через `i18n.tr` и содержат предупреждение. В списке ключей — `KeyInfo.as_dict()`: там `id`, `prefix`, даты, состояние, права, scope, квоты и **нет** верификатора.
- **Перевод:** в `i18n`-каталог надо добавить `E_AUTH_DISABLED` (см. §5) и, по желанию, `detail`/`action` из `ApiKeyError`. Сам модуль тексты только переводит, словарь не трогает.

### 1.4 `proxy_workbench/secrets.py` — владелец `secrets.py`

- Граница зафиксирована и проверена тестом: `ApiKeyManager` **не принимает, не хранит и не возвращает** открытый пароль. `create()` не имеет параметра `password` (TypeError), в таблице `api_keys` нет колонки под plaintext (тест `test_apikeys_secrets.py::test_there_is_no_column_that_could_hold_a_plaintext_secret`). Upstream credentials, которым нужен обратный доступ transport, остаются в OS vault у вас; подставлять их вместо API-ключа нельзя.
- `access_revision` (CONTRACTS §8.2) от меня не зависит и не блокирует ключи: ключ ограничивается `collection_id`/`pool_id` из §5.3, а не ревизией доступа.

## 2. Что уже сделано у меня

Публичный API `proxy_workbench/apikeys.py`:

| Имя | Назначение |
| --- | --- |
| `ApiKeyManager(conn, *, now, iterations, touch_interval_s, audit_retention)` | Сервис. Соединение принадлежит вызывающему; `now` внедряется, поэтому expiry и окно ротации проверяются без `sleep`. |
| `.bootstrap_admin(*, local_trusted, name, purpose, expires_at, ttl_s, permissions, scope, rate_limit, concurrency)` | Первая админ-ключ только с локального доверенного канала. |
| `.create(*, actor, name, purpose, expires_at, ttl_s, permissions, scope, rate_limit, concurrency)` | Выдача ключа. `actor` обязан иметь `admin.keys`. |
| `.list_keys(*, actor, state, limit)`, `.get_key(key_id, *, actor)` | Список и карточка: `id`, `prefix`, даты, состояние, права, scope, квоты. |
| `.update_metadata(key_id, *, actor, name, purpose, expires_at, ttl_s)` | Только переименование и срок. Права и scope — не редактируются здесь. |
| `.disable`, `.enable`, `.revoke`, `.delete`, `.rotate(key_id, *, actor, grace_s=0.0)` | Жизненный цикл. `revoke` терминален, `delete` убирает строку и оставляет audit. |
| `.authenticate(secret, *, permission, include_secrets, collection_id, pool_id, allow_grace, touch, now)` | Проверка секрета + состояния + прав за один вызов. |
| `.assert_active(key_id, *, operation, now)` | Перечитка состояния для lease и для открытого потока. |
| `.purge_expired_grace(now=None)` | Уборка superseded-верификатора закрывшегося окна. |
| `.read_audit(*, actor, key_id, operation, since, limit)` | Чтение журнала; требует `admin.audit`. |
| `QuotaGuard(manager)` → `.check()`, `.acquire()`, `.active_count()`, `.forget()`; `Lease` | Скользящее окно запросов и слоты параллелизма. `Lease` — контекстный менеджер, `revalidate()` перечитывает ключ. |
| `StreamSessions(manager, *, recheck_interval_s)` → `.open()`, `.sweep()`, `.due()`, `.active()`; `StreamSession` | Политика SSE: сессия перечитывает ключ при открытии, перед каждым событием и при `sweep()`; отзыв закрывает её с `E_AUTH_REVOKED`. |
| `authorize`, `object_visible`, `effective_collections`, `visible_collections`, `visible_pools` | Object-level проверки, которые фильтры не могут расширить. |
| `Scope`, `RateLimit`, `Concurrency`, `KeyInfo`, `IssuedKey`, `Principal`, `Decision`, `ApiKeyError` | Узкие типы, которые возвращает сервис. |
| `PERMISSIONS`, `ADMIN_PERMISSIONS`, `READ_PERMISSIONS`, `SENSITIVE_PERMISSIONS`, `HTTP_STATUS`, `ensure_schema` | Канон, который нужен потребителям. |

Формат секрета: `pwk_<12 hex-символов>_<43 символа base64url>`. Handle (`prefix`) — свойство **ключа**, а не секрета, поэтому он не меняется при ротации и старый секрет остаётся находимым по индексу `api_keys(prefix)` внутри окна. 256 бит энтропии за handle; сравнение — `hmac.compare_digest`, для неизвестного handle выполняется та же PBKDF2-работа с фиктивной солью.

## 3. Совместимость

- **Что ломается, если §1.1 не внести:** миграция 8 без четырёх колонок даёт `disable`/`rotate(grace_s>0)` без опоры. `ensure_schema` это чинит сам и идемпотентно, но тогда схема определяется в двух местах.
- **Что НЕ ломается:** обратная совместизация — модуль новый, существующие тесты (`tests/test_api.py`, `tests/test_workbench.py`, `tests/test_gui.py`) не затрагиваются; `api.py`, `proxytool.py`, `gui.py`, `ui/*`, `secrets.py` я не менял; данные старых баз не затрагиваются — таблицы новые.
- **`disabled_at` отсутствующий:** `KeyInfo` читает отсутствующую колонку как `None`, а `_as_record()` подставляет её, поэтому старый список ключей на старой схеме покажет `disabled_at: null`, а не упадёт (тест `test_ensure_schema_adds_a_column_to_an_older_table`).

## 4. Проверки

Выполнены в этой сессии, из корня репозитория:

```
.venv/bin/python -m unittest tests.test_apikeys_secrets       → Ran 20 tests, OK
.venv/bin/python -m unittest tests.test_apikeys_permissions  → Ran 23 tests, OK
.venv/bin/python -m unittest tests.test_apikeys_lifecycle    → Ran 28 tests, OK
.venv/bin/python -m unittest tests.test_apikeys_quota        → Ran 25 tests, OK
.venv/bin/python -m unittest tests.test_apikeys_audit        → Ran 13 tests, OK
.venv/bin/python -m unittest tests.test_apikeys_schema       → Ran  5 tests, OK
```

`tests/test_apikeys_schema.py` — единственный файл, который работает с чужим модулем: он вызывает `db.migrate()` и `db.connect()` и проверяет, что `apikeys` живёт на реально созданной схеме. Он ничего не меняет в `db.py` и падает в `skip`, если `db.py` ещё нет в дереве.

**Что осталось непрочитанным:** полный `unittest discover -s tests` не запускался — по HANDOFF §5.1 его запускает интегратор, а параллельная половина дерева сейчас чужая и незаконченная.

## 5. Открытые вопросы

1. **`E_AUTH_DISABLED` — новый код в домене `AUTH`.** §5.4 перечисляет `E_AUTH_REVOKED`, но не различает «отозван навсегда» и «выключен временно». Я выбрал отдельный код, потому что `E_AUTH_REVOKED` заставил бы клиента считать временно выключенный ключ мёртвым навсегда. `ApiKeyError.state` в любом случае отдаёт `disabled`/`revoked`, поэтому решение интегратора может быть и обратным: свернуть в `E_AUTH_REVOKED` и оставить различие в `state`. Мне нужен ответ, какой вариант канон.
2. **Узкое read-only subscription secret** (§5.1, для клиентов без заголовков) я **не делал**: в моей задаче его нет, а лишний тип ключа без потребителя — это лишняя поверхность. Если `apiv1.py` его нужен, прошу сформулировать требование (срок, redaction, допустимые права) отдельным handoff: правильно выдать такой секрет можно только вместе с правилами, кому он выдаётся.
3. **Кто зовёт `purge_expired_grace()`** — планировщик процесса или сам `apiv1.py` по своему таймеру. Я оставил метод явным и не стал запускать его из фонового потока внутри сервиса: тихий фоновой писатель в чужой БД — это ровно тот конфликт, который §3.6 CONTRACTS запрещает.
