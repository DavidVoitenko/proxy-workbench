# Handoff: apiv1

**Требования:** F29 (версионированный control API `/v1` и операции), F18 (единый service layer), F07 (параметр не игнорируется молча), R03 (API отзывчив во время scan), R12 (LAN по умолчанию и общий секрет), R17 (structured events вместо разбора лога), R18 (quick test подписан бюджетом)
**Контракт:** CONTRACTS.ru.md §5 целиком, §6.2, §7.7 (версия 1); §3.3 миграции 8 и 10 — через `apikeys.py`
**База:** ветка `integration/ultra-2026-09-25`, `apikeys.py`, `db.py`, `core.py`, `jobs.py` уже лежат в дереве

## 1. Прошу внести в чужие файлы

### 1.1 `proxy_workbench/api.py` (интегратор): поднять `/v1` рядом с legacy-чтением

- `api.py:249` `make_api_server` — рядом с существующим read-only сервером должен подниматься `apiv1.ApiV1(service=..., keys=apiv1.ApiKeyStore(manager))`. Старые `/proxies`, `/random`, `/status`, `/pac`, `/clash`, `/singbox` остаются как есть.
- `api.py:280` `authorized()` — token-in-query сегодня работает молча. Прошу отдавать те же заголовки, что и `apiv1` для совместимого пути: `Deprecation: true` и `Warning: 299 - "a token in the query string is deprecated; send Authorization: Bearer instead"`, и учитывать это в счётчике отказов. Тихая потеря старой интеграции запрещена, но и рекламировать query-токен нельзя.
- `api.py:93` `Exports` — не переименовывать: это источник строк для service layer (§1 CONTRACTS). `apiv1` его не импортирует и не дублирует: строки приходят из `Service.invoke('results.list', ...)`.

### 1.2 `proxy_workbench/proxytool.py` (интегратор): реализовать `apiv1.Service`

Мой модуль не знает, где живут коллекции, профили и результаты. Ему нужен один класс с двумя методами:

```python
class Service:                       # apiv1.Service, apiv1.py:444
    def invoke(self, operation: str, call: apiv1.Call) -> dict
    def queue_state(self) -> {'depth': int, 'capacity': int | None}
```

`apiv1.Call` (apiv1.py:828) несёт: `operation`, `principal` (уже проверенный и разрешённый), `params` (path-параметры), `query` (проверенные фильтры + `cursor`/`cursor_stream`/`cursor_seq` для постраничных), `body` (проверенное тело), `idempotency_key`, `expected_revision`, `deadline_s`, `path`, `method`.

Что сервисный слой обязан вернуть (иначе `apiv1` отвечает 503 `E_SERVICE_UNAVAILABLE`):

| Вид ответа | Контракт |
| --- | --- |
| обычный | JSON-объект; в нём не должно быть полей вне scope ключа (это дополнительно проверяет `apiv1._guard_scope`) |
| постраничный | `{'items': [...], 'stream_id': str, 'next_seq': int | None}`; курсор кодирует `apiv1` |
| долгий | `job_id` обязателен, ответ 202 и `Location: /v1/jobs/<id>` |
| файл | `{'data': bytes, 'content_type': str, 'filename': str}` для `exports.download` |
| события | для `jobs.events` / `events.system`: итератор событий либо кортеж `(stream_id, итератор)`; событие обязано иметь строго растущие `seq` и `type` |

`queue_state()` вызывается перед каждой долгой операцией: при `depth >= max_queue_depth` (по умолчанию 100) `apiv1` отвечает 429 `E_LIMIT_QUEUE` с `Retry-After`, до вызова сервиса.

### 1.3 `proxy_workbench/apikeys.py` (владелец ключей): два запроса

1. **Публичная запись в audit.** `apiv1.ApiKeyStore.audit` (apiv1.py:587) сейчас вызывает `manager.record_audit(...)`, если он есть, иначе приватный `manager._audit(...)`. Прошу сделать `record_audit(key_id, operation, *, object_kind=None, object_id=None, scope=None, result='ok', error_code=None)` публичным — иначе чужой модуль зовёт приватный метод.
2. **Права и scope существующего ключа.** `update_metadata` (apikeys.py, «Rights, scope and quotas are not editable here») и я поэтому сузил тело `PATCH /v1/keys/{id}` до `name`/`purpose`/`expires_at`. Это осознанно: `apiv1` не обещает того, чего менеджер не умеет. Если расширение нужно — менять `apikeys`, и тогда расширить тело маршрута, а не наоборот.

`ApiKeyManager` уже подходит по форме: `authenticate`, `list_keys`, `get_key`, `create`, `update_metadata`, `rotate`, `revoke`, `disable`, `enable`, `delete`, `read_audit` — все используются адаптером `apiv1.ApiKeyStore` без копирования логики.

### 1.4 `docs/` (владелец документации, `desktop.py` по HANDOFF §1.2): страница «API и ключи»

F29 требует страницу с примерами curl/Python/JS и законченный сценарий. Сценарий уже расписан в docstring `proxy_workbench/apiv1.py:1-33`, тексты ошибок берутся из `apiv1.MESSAGES` через существующий `i18n.tr`. Прошу перенести это в README/SECURITY без изменения смысла и без обещания «Bearer = шифрование».

### 1.5 `docs/integration/CONTRACTS.ru.md` (интегратор): три кода в §5.4

Канон §5.4 перечисляет «примеры кодов», но моя поверхность обязана отвечать ими:

| Код | HTTP | Почему нужен |
| --- | --- | --- |
| `E_VALIDATION_METHOD` | 405 | маршрут есть, метод другой; в ответе `Allow` |
| `E_STATE_NOT_FOUND` | 404 | объект не найден **или** вне scope ключа — код один и тот же, иначе ключ узнаёт о чужом scope |
| `E_SERVICE_UNAVAILABLE` | 503/500 | операция не подключена к service layer либо вернула не тот ответ |

Плюс `E_AUTH_DISABLED` — уже есть у `apikeys` (состояние `disabled`), я его только переиспользую. Ни один существующий код не менял.

## 2. Что уже сделано у меня

- `proxy_workbench/apiv1.py` — 100 маршрутов `/v1` во всех 13 областях F29, `ApiError`/`MESSAGES` (32 кода), валидация тела и query по закрытым наборам полей, `Idempotency-Key`, `If-Match`/`revision`, `ETag`/`304`, курсор `(stream_id, seq)` с привязкой к набору фильтров, SSE с ограниченной историей и перепроверкой ключа, rate-limit/queue/concurrency/body лимиты, `Deprecation`+`Warning` для legacy query-токена, редирект-обёртка `Principal.redacted` для подписки и legacy.
- `proxy_workbench/openapi.json` — артефакт, сгенерированный из `apiv1.openapi_document()`; тест сверяет файл с таблицей маршрутов, так что рассинхрон ловится сразу.
- `apiv1.ApiKeyStore(manager)` — адаптер к `apikeys.ApiKeyManager`; `apiv1.Service` — протокол сервисного слоя.
- Тесты: `tests/test_apiv1_service.py` (14), `tests/test_apiv1_auth.py` (12), `tests/test_apiv1_control.py` (20), `tests/test_apiv1_limits.py` (15), `tests/test_apiv1_apikeys.py` (7, на реальном `ApiKeyManager` и временной БД).

## 3. Совместимость

- Ни один существующий файл не изменён: `apiv1` ничего не импортирует из `api.py`, `gui.py`, `proxytool.py` на верхнем уровне; `canon_sorts()`/`canon_protocols()` импортируют `proxytool` лениво и только за списком сортировок.
- `api.py` и `proxytool.py` не должны начать зависеть от `apiv1` иначе, чем через `ApiV1(service=..., keys=...)` и `Service.invoke`: иначе получится второй диспетчер операций, который запрещён HANDOFF §2.
- Канон сортировок взят из `proxytool.SORTS` (а не задан вторым списком). Если он изменится — OpenAPI и таблица маршрутов поедут автоматически, а тест `test_apiv1_service` это не заметит; это осознанный риск, закрытый одним импортом.
- `tests/test_freshness.py:87`, `tests/test_selection.py:275`, `tests/test_anonymity.py:206` я не трогал: это замечания §8.3 CONTRACTS, и переписывает их владелец теста.

## 4. Проверки

Выполнены в этой сессии, каждая отдельно:

```
.venv/bin/python -m unittest tests.test_apiv1_service    → Ran 14 tests, OK
.venv/bin/python -m unittest tests.test_apiv1_auth      → Ran 12 tests, OK
.venv/bin/python -m unittest tests.test_apiv1_control   → Ran 20 tests, OK
.venv/bin/python -m unittest tests.test_apiv1_limits    → Ran 15 tests, OK
.venv/bin/python -m unittest tests.test_apiv1_apikeys  → Ran 7 tests, OK
```

Полный `unittest discover -s tests` не запускался: по условию задачи его гоняют другие исполнители, и красный результат из-за их незаконченной работы мне не принадлежит.

## 5. Открытые вопросы

1. **Кто пишет `Service`.** Это F18 и общий service layer; в моём задании его нет, и без него `/v1` не отвечает ни на одну доменную операцию. Просьба к интегратору: §1.2.
2. **`access_revision` (§8.2 CONTRACTS).** `apiv1` передаёт `access_id`/`access_revision` только как данные; отдельного маршрута доступа у меня нет, потому что сущности ещё нет. Когда появится `secrets.py` + `db.py` миграции 3 — понадобится решение, где именно живёт выдача `access_id` (мой контракт: `redacted`-поля `access_secret`/`credentials` вычищаются для подписки и legacy).
3. **Rate-limit по ведро́там.** Сейчас окно считается на паре (ключ, право операции), а не суммарно на ключ. Если нужен общий предел на ключ, это одна правка в `ApiV1._rate_check` — но она меняет смысл `rate_limit_json` из миграции 8, поэтому решение за владельцем `apikeys`.
4. **Двоевластие привязки шлюза.** `gateway.config_set` пропускает `listen_host`, только если он loopback или перечислен в `ApiConfig.allowed_bind_hosts`. Кто наполняет этот список (CLI-флаг обратного прокси или настройка) — решение поверхности `gateway`.
