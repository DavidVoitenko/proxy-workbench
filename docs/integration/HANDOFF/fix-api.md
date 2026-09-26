# Handoff: fix-api (api.py + apiv1.py)

**Владею:** `proxy_workbench/api.py`, `proxy_workbench/apiv1.py`. Больше ничем.
**Ветка:** `integration/ultra-2026-09-25`.
**Контракт:** CONTRACTS.ru.md §5 (версия 1), §5.2 права/квоты, §5.3 resource scope, §5.4 коды ошибок.
**Как проверялось:** живыми HTTP-запросами к поднятому продукту
(`.venv/bin/python -m proxy_workbench serve --data <временная папка> --port 18790`),
а не только юнит-тестами. Ниже каждый пункт «было/стало» помечен живым запросом.

---

## 0. Кратко: что оказалось не так с постановкой

| # | Дефект из задания | Воспроизводился ли на HEAD |
|---|-------------------|------------------------------|
| 1 | Утечка scope через список заданий | **Нет** — уже исправлено коммитом `01faf77` |
| 2a | Обход scope при скачивании артефакта | **Нет** — уже исправлено тем же коммитом |
| 2b | `raw_body` отдаёт байты до `_redact` | **Да** — живой запрос, исправлено |
| 3 | Квота concurrency не проверяется | **Да**, и хуже: квоту вообще нельзя задать через API |
| 4 | `reservations` — заглушки | **Да** — живой запрос, исправлено |

Пункты 1 и 2a я перепроверил руками и оставил как есть: код там уже правильный, ломать было нечего.
Подробности — §5.

---

## 1. Что изменено в `apiv1.py`

### 1.1 Редактирование сырого файла (`_response`, `raw_body=True`)

`GET /v1/exports/{id}/download/{name}` отдавал `result['data']` байтами, **не пропустив их ни через
`_redact`, ни через `_guard_scope`**. `_guard_scope` в текущем HEAD уже вызывается (это сделал `01faf77`),
`_redact` — нет.

Воспроизведено живьём. Строка результата, у которой есть поле `access_secret`
(оно в `apiv1.REDACTED_FIELDS`, но **не** в `exportsvc.REDACTED_KEYS`), попадала в `ranked.json`:

```
GET /v1/exports/{artifact}/download/ranked.json  (subscription-секрет)
  до:  200, в теле "access_secret": "ACCESS-SECRET-VALUE-7f2c"
  после: 200, поля нет
GET /v1/results   (тот же ключ)  — поля не было и до, и после
```

Сделано:

* `_redact(result, principal)` и `_guard_scope(principal, result)` выполняются **до** чтения `result['data']`.
* Новая функция `_redact_file(data, content_type, principal)` — для JSON-файла разбирает тело и
  прогоняет через ту же `_redact`, что и JSON-путь. Тем же правилом, тем же списком полей.
  Для не-JSON форматов тело не трогается: в артефакте это `scheme://host:port` построчно
  (`proxies.txt`, `hostport.txt`, `http.txt`, `proxychains.txt`, `proxy.pac`, `clash.yaml`),
  фиксированный список колонок (`ranked.csv`) или шапка комментария (`snapshot.txt`) —
  переписывать их в API-слое значило бы завести вторую грамматику экспортёра.
* Ответ сервиса может пометить файл как несущий секреты (`secrets: True`). Для `redacted`-идентичности
  (subscription / legacy) такой файл **отказывается** `E_AUTH_PERMISSION` 403, а не отдаётся частично
  вычищенным. `api.py::_op_exports_download` выставляет этот флаг, читая `credentials` из `status.json`
  **самогенерации** (не изменяемую копию в корне `exports/`).
* Контент-тайп файла теперь известен (`ARTIFACT_CONTENT_TYPES` в `api.py`), раньше файл отдавался
  всегда как `application/octet-stream`.

### 1.2 Квота concurrency: создаётся и действует

Тут было два дефекта, а не один.

**(а) Квоту нельзя было задать через API.** `KEY_BODY` объявляет поле `concurrency`, но
`ApiKeyStore.create_key` передавал его в `apikeys` голым целым числом, а `apikeys.Concurrency.of`
требует отображение. Живой запрос:

```
POST /v1/keys {"name":"x","permissions":["read.results"],"concurrency":1}
  до: 400 E_VALIDATION_FIELD   (поле объявлено собственной таблицей маршрутов)
  после: 200 {"concurrency": {"max_active": 1}}
```

То же было и с `rate_limit`: в `create_key` передавался кортеж `(N, S)`, а `RateLimit.of` тоже
требует отображение — квота запросов в секунду была неустанавливаема через API целиком.
Оба перевода теперь в адаптере (единственное место, где словарь маршрута сходится со словарём `apikeys`).

**(б) Квоту никто не читал.** Добавлен `apiv1.KeyQuota` — счётчик одновременных операций **на ключ**
(`Principal.concurrency` → `max_active`), в отличие от `ConcurrencyLimiter`, который ограничивает
весь сервер (16) и к квоте ключа отношения не имеет. Слот берётся в `_invoke` рядом с серверным
слотом и отдаётся в том же `finally`; оба лимитера не могут утопить друг друга, потому что `key_slot`
связывается только после того, как ответили оба. Для SSE слот держится **всю жизнь потока** и
возвращается в `finally` генератора — закрытая подписка не должна навсегда заблокировать ключ.

Живой запрос (ключ с `max_active=1`):

```
8 одновременных GET /v1/results  ->  [200,200,200,200,200,200,429,429]
                                    429 несёт Retry-After: 1
после пачки                      ->  200 (слот вернулся)
```

**Честное уточнение по критерию приёмки.** В задании сказано «8 *последовательных* запросов → 429».
При настоящей квоте одновременных операций последовательные запросы дают 8×200: каждый отпускает
слот до следующего. Это и есть смысл `max_active` («сколько запросов/подписок/аренд ключ держит
одновременно», apikeys.py:423). Квота на окно времени — это `rate_limit`, и она работает:

```
ключ с rate_limit {requests: 5, window_s: 60}:  [200,200,200,200,200,429,429,429]
```

Заставить последовательные запросы давать 429 можно было бы только утечкой слота — это был бы новый
дефект (ключ блокируется навсегда после одного запроса), поэтому так я не делал.

### 1.3 Мелочь, найденная по дороге

`ApiKeyStore._principal_of` уже переносит `concurrency` в `Principal` — сломано было только создание
и только чтение. Правок в `_principal_of` не потребовалось.

---

## 2. Что изменено в `api.py`

### 2.1 Резервации стали настоящими арендами

`_op_reservations_acquire/lease/release` отдавали первые N строк текущего снапшота и **игнорировали
всё тело**: `lease_id`, `ttl_s`, `state`. Ничего не резервировалось. Живой запрос на HEAD:

```
acquire #1 -> 200 items=[203.0.113.9, 203.0.113.8]   (пул привязан к коллекции theirs!)
acquire #2 -> 200 items=[203.0.113.9, 203.0.113.8]   тот же адрес дважды
lease(lease_id='lease-I-MADE-UP')  -> 200 с живыми прокси
release(lease_id='lease-I-MADE-UP')-> 200
lease_id в ответе нет ни разу
```

Сделано:

* Новый класс `Reservations` (реестр аренд) в `api.py`, живёт в `WorkbenchService`, то есть в процессе,
  который и раздаёт адреса.
  * `acquire(pool_id, key_id, candidates, count, ttl_s)` — берёт только те адреса, которых **никто
    сейчас не держит**; выдаёт `lease_id`, `acquired_at`, `expires_at`, `state`.
  * `renew(lease_id, pool_id, key_id, ttl_s)` — продлевает TTL существующей аренды.
  * `release(lease_id, pool_id, key_id, state)` — возвращает адреса, `state` выбирается из
    `('returned','lost','consumed')` и попадает в ответ.
  * Владение — по ключу. Чужая аренда и несуществующая отвечают одинаково: `E_STATE_NOT_FOUND`/404.
  * Истечение TTL освобождает адреса лениво (проверка при следующем обращении), без sweeper'а:
    остановленный процесс не может «застрять» с вечной арендой.
  * Когда свободных адресов не хватает — `E_LIMIT_QUEUE` 429 с `Retry-After`, равным времени до
    самого раннего истечения. Нового кода ошибок не заводил: `openapi.json` проверяется тестом
    `tests/test_apiv1_service.py:231` на равенство `openapi_document()`, а файл не мой.
* `_pool_candidates()` — строки теперь **этого пула**: `pool_member` (только `active`/`reserve`),
  склеенные с `results` по профилю пула, и пропущенные через общий контракт допуска
  (`core.select` + `exportsvc.attach_admission`), чтобы арендованный адрес и скачанный не расходились
  во мнении о возрасте и вердикте. Сеть измерения берётся из конфигурации профиля
  (`proxytool.snapshot_network`), а не угадывается.
* `_op_reservations_feedback` больше не отвечает тем же, что acquire. Теперь применяет отзыв к фазе
  участника пула (`ok` → `active`, не `ok` → `cooldown`). Поля `latency_ms`, `target_id`, `error_code`
  **негде хранить** (таблицы `feedback` в схеме нет) — они возвращаются в ответе как
  `not_stored: {fields, reason}`, а не молча выбрасываются (F07).

Живой запрос после правки:

```
acquire #1 -> 200 lease-11b0b4ba7c6126c5 items=[10.55.0.1, 198.51.100.21] ttl_s=30
acquire #2 -> 200 lease-3104bcffd460c4b4 items=[198.51.100.22, 198.51.100.23]  overlap = ∅
дренирование пула        -> 429, Retry-After: 29
lease(lease-INVENTED)    -> 404
release(lease-INVENTED)  -> 404
чужой ключ renew/release -> 404
свой release state=consumed -> 200, state в ответе = consumed
после release адрес вернулся в пул
```

### 2.2 Флаг секретности артефакта

`_op_exports_download` теперь возвращает `content_type` и `secrets` (см. §1.1).

---

## 3. Прошу внести в чужие файлы

### 3.1 `proxy_workbench/exportsvc.py` — выровнять списки запрещённых полей

`apiv1.REDACTED_FIELDS` = `secret, password, credentials, upstream_credential, access_secret,
gateway_password, verifier, verifier_salt, authorization`.
`exportsvc.REDACTED_KEYS` = `password, passwd, pass, username, user, login, token, secret, api_key,
apikey, credential, credentials, auth, secret_ref`.

Расхождение в обе стороны: `exportsvc` не знает `upstream_credential`, `access_secret`,
`gateway_password`, `verifier`, `verifier_salt`, `authorization`. Пока расхождение есть, JSON-файл
артефакта (`ranked.json`) может вынести такое поле, и чинить его придётся в API-слое — то есть
гарантия держится на втором месте, а не на источнике. Прошу взять `apiv1.REDACTED_FIELDS` как
подмножество и добавить недостающие пять. Тогда правка в `apiv1._redact_file` останется страховкой,
а не единственной линией обороны.

### 3.2 `proxy_workbench/db.py` — таблица аренд и таблица отзывов

1. **Аренды живут в памяти процесса.** Этого достаточно для одного сервера (а это и есть текущая
   модель сети: один процесс слушает loopback), но перезапуск забывает аренды, и два процесса на
   одной базе выдали бы один адрес дважды. Нужна таблица вида
   `reservation_lease(lease_id, pool_id, key_id, state, acquired_at, expires_at, released_at)` плюс
   `reservation_item(lease_id, endpoint_id)`, и `Reservations` должен читать/писать её вместо словаря.
   Я готов перейти на неё, как только миграция появится; сейчас DDL в `db.py` не пишу.
2. **`POST /v1/reservations/feedback` не может хранить `latency_ms`/`target_id`/`error_code`.**
   Таблицы `feedback`/`pool_feedback` в схеме нет. Нужна
   `pool_feedback(pool_id, endpoint_id, at, ok, latency_ms, target_id, error_code)`.
   Пока её нет, ответ честно перечисляет несохранённые поля.

### 3.3 `proxy_workbench/proxytool.py` и `proxy_workbench/profiles.py` — профиль из API не годится для экспорта

Это не в моих четырёх пунктах, но я на это наткнулся, когда поднимал продукт, и без этого
`POST /v1/exports` не работает из API вообще.

```
POST /v1/profiles {"name":"p","targets":[{"id":"t1","kind":"required","min_success":1.0}]}
  -> 200 {"id": "p_72b2dd79002ca034", ...}
POST /v1/exports {"profile_id": "p_72b2dd79002ca034", ...}
  -> 500 E_SERVICE_UNAVAILABLE, причина скрыта: ValueError('Профиль проверки не найден')

POST /v1/exports {"profile_id": "p_72b2dd79002ca034@1", ...}
  -> 500 E_SERVICE_UNAVAILABLE, причина скрыта: KeyError('url')
```

Две несовместимости:

* `api.py::_op_profiles_create` пишет в ту же таблицу `profiles` строку с id вида `<p_xxx>@<revision>`,
  а `proxytool.export` ищет `SELECT 1 FROM profiles WHERE id=?` — content-addressed id
  (`sha256(config)[:20]`, `proxytool.py:1593`). Идентификатор, который API отдал пользователю,
  экспорт не находит.
* `api.py::spec_of` кладёт в конфиг таргеты вида `{id, kind, min_success, enabled}`, а
  `proxytool.export` читает `cfg['targets'][*]['url']` (`proxytool.py:2469`).

То есть профиль, созданный через API, нельзя экспортировать и нельзя предъявить движку. Прошу
согласовать форму: либо `spec_of` переводит таргеты в форму движка (`name` + `url`), либо
`export()` принимает оба вида. Пока не согласовано, `_op_exports_create` падает в 500 с
`reason: KeyError`/`ValueError` — то есть внятного кода ошибки у пользователя нет.

### 3.4 `proxy_workbench/proxytool.py` — `POST /v1/pools` требует `profile_id`, а объявляет его необязательным

```
POST /v1/pools {"name":"p","collection_id":"c","desired":5}  -> 422 E_VALIDATION_FIELD: profile_id is required
POST /v1/pools {..., "profile_id": "<id>"}                     -> 200
```

Либо `profile_id` должен стать обязательным в `ROUTES` (и в OpenAPI), либо `_op_pools_create`
должен подставлять профиль по умолчанию, как это делает `_op_checks_collect`
(`self.published_profile_id()`). Сейчас объявление и поведение расходятся, а это F07.

### 3.5 Тесты, которых нет (побочный продукт, а не результат)

Мои два файла — единственное, что мне разрешено править, поэтому тестов я не добавлял.
Что стоит покрыть, когда тесты снова можно будет трогать:

* `test_apiv1_scope.py`-подобный тест: подписка не получает `access_secret` из
  `GET /v1/exports/{id}/download/ranked.json` (уже есть `test_subscription_answers_are_redacted`
  только для JSON-пути — `tests/test_apiv1_auth.py:263`).
* `POST /v1/keys` с `concurrency` и `rate_limit_requests` больше не 400.
* `KeyQuota`: N одновременных запросов при `max_active=K` дают ровно K ответов 200 и остальные 429
  с `Retry-After`; после пачки счётчик пуст.
* `Reservations`: два acquire одного пула не пересекаются; чужой `lease_id` → 404; TTL освобождает
  адрес; `state` попадает в ответ.
* `api.py::_op_reservations_acquire` на пуле без участников обслуживающего состояния.

---

## 4. Проверка, которую я прогнал (живой продукт, временная папка данных)

Подъём: `serve --data <tmp> --port 18790 --api-token <t>`, админский ключ через
`python -m proxy_workbench api-key bootstrap --name ...`. Две коллекции `mine` и `theirs`,
в `theirs` измерен адрес, ключ со `scope.collections = [mine]`.

```
=== scope: ключ не видит theirs ===
GET /v1/jobs                                    -> 200, только mine
GET /v1/results?limit=50                        -> 200, только mine
GET /v1/collections                             -> 200, только mine
GET /v1/collections/{theirs}                    -> 404
GET /v1/collections/{theirs}/members            -> 404
GET /v1/exports/{артефакт theirs}              -> 404
GET /v1/exports/{артефакт theirs}/compatibility -> 404
GET /v1/exports/{артефакт theirs}/download/proxies.txt -> 404
GET /v1/exports/{свой артефакт}/download/proxies.txt   -> 200, тело своё
GET /v1/jobs/{задание theirs}                   -> 404
админ без scope видит обе коллекции              -> 200

=== редактирование подписки ===
артефакт с credentials=reference + подписка    -> 403 E_AUTH_PERMISSION
обычный артефакт + подписка (ranked.json)      -> 200, access_secret отсутствует
тот же файл обычным ключом                    -> 200, access_secret присутствует

=== concurrency ===
POST /v1/keys {"concurrency": 1}               -> 200 {"max_active": 1}
8 одновременных GET /v1/results                -> 429 присутствует, Retry-After: 1
следующий запрос                                -> 200

=== reservations ===
два acquire одного пула                         -> разные адреса, пересечения нет
выдуманный lease_id                            -> 404 на renew и на release
чужой ключ                                     -> 404
свой release                                   -> 200, state=consumed
```

Регрессии: `tests/test_apiv1_control.py`, `test_apiv1_auth.py`, `test_apiv1_service.py`,
`test_apiv1_apikeys.py`, `test_apiv1_scope.py`, `test_apiv1_limits.py`, `test_api.py`,
`test_pools_api.py`, `test_web_readpath.py`, `test_web_results.py` — зелёные.

---

## 5. Пункты 1 и 2a: почему я их не трогал

Оба уже исправлены коммитом `01faf77` («Ремонт по находкам ревью») — тем, который добавил
`_guarded_artifact`, `SCOPE_ENVELOPES` и разбор `kind not in SCOPE_KINDS` в `_guard_objects`.
Живая проверка на HEAD, до моих правок:

```
ключ со scope=[mine]
GET /v1/jobs                                    -> 200, 1 элемент, scope.collection_id = mine
GET /v1/jobs/{задание theirs}                   -> 404
GET /v1/profiles|sources|schedules              -> 200, пусто (у них нет своей коллекции)
ключ без scope
GET /v1/jobs                                    -> 200, обе коллекции
GET /v1/exports/{артефакт theirs}/download/...  -> 404
```

Причина, по которой `_scope_values(principal, 'jobs')` **не** возвращает `None`, хотя у
`apiv1.Principal` нет ни `resource_scope`, ни `jobs`: функция нормализует неизвестный вид к
`collections` (`api.py:2617`) и читает `getattr(principal, 'collections')` — а такое поле у
`apiv1.Principal` есть, и `ApiKeyStore._principal_of` его заполняет. Описание корневой причины в
задании, судя по всему, относится к более ранней ревизии.

Что я всё же поправил по этой теме, потому что это была дыра: `profiles`, `sources` и `schedules`
отфильтровываются по коллекции объекта, а у них коллекции нет (`_collection_of` → `None`), поэтому
ключ со scope видел **пустой** список, а не «своё». Это безопасно, но неверно по смыслу: расписание,
привязанное к пулу ключа, должно быть видно. Сейчас это так и есть — просто не видно ничего лишнего.
Правку не делал: она требует решить, чей это дефект (`pools.py` / `scheduler.py` — не мои файлы,
и решение «список пуст = не показывать» может быть осознанным), и она не входит в четыре заявленных
пункта. Записал сюда, чтобы решение не потерялось.
