# fix-keys.md — что осталось за пределами `apikeys.py` и `db.py`

Ветка `integration/ultra-2026-09-25`. Владелец: `proxy_workbench/apikeys.py`,
`proxy_workbench/db.py`. Всё, что ниже, требует правки в чужом файле и **не
сделано** — сделано всё, что было возможно сделать из двух своих файлов.

Пять подтверждённых дефектов, что сделано и что нужно от владельца:

| # | Дефект | Состояние |
|---|--------|-----------|
| 1 | секрет ключа возвращается дважды через idempotency-кэш | на своей стороне готов API, нужна 1 правка в `apiv1.py` (раздел 1) |
| 2 | `purge_expired_grace` никогда не вызывается | **закрыт** в `apikeys.py` |
| 3 | `rebind_secrets` не поднимает ревизию | **закрыт** в `db.py`, 2 теста фиксируют старый контракт (раздел 3) |
| 4 | retention: preview и apply расходятся | **закрыт** в `db.py` |
| 5 | промежуточная база не обновляется и без копии | **закрыт** в `db.py`, причём было хуже отчёта (раздел 5) |

---

## 1. `apiv1.py` — не кэшировать ответ, который несёт секрет

### Что сломано

`ApiV1._invoke` (`proxy_workbench/apiv1.py`, строка
`self.idempotency.put(bucket, idem_key, digest, response)` — на момент написания
2044; файл в этот момент правят другие владельцы, поэтому ищите по тексту):

```python
response = self._response(route, request, result, principal, extra_headers, query)
if idem_key:
    self.idempotency.put(bucket, idem_key, digest, response)
```

`Response` — `@dataclass(frozen=True)` с неизменяемым `body: bytes`
(`class Response`, ~896). Тело собрано **до** `put`, и это же самый объект
возвращается клиенту. Повтор того же `POST /v1/keys` с тем же `Idempotency-Key`
и тем же телом отдаёт из кэша байт-в-байт тот же ответ — с полным секретом,
хотя в `api_keys` по-прежнему одна строка. Воспроизведено:

```
first  -> 200 secret=pwk_82061a7404b7...
replay -> 200 secret=pwk_82061a7404b7...  SAME FULL SECRET TWICE: True
rows in api_keys: 2  (одна строка на два ключа админа + один выданный, две выдачи)
```

### Почему нельзя починить из `apikeys.py`

Проверено, а не предположено:

* `json.dumps` тела происходит один раз, в `_response`; дальше только
  `bytes`. Секрет материализован до того, как `apikeys` получает хоть какую-то
  возможность что-то сказать.
* `put` и возврат клиенту — **один и тот же объект** `Response`. Если изменить
  его между `put` и сетевой записью, первый ответ потеряет секрет тоже.
* Повторный запрос не доходит до сервисного слоя: кэш проверяется в `_invoke`
  до `_parse_body`, до `_key_call`, до всего, что принадлежит `apikeys`.
* В `authenticate` повторного запроса попадает только админский ключ, и он
  легитимен — отказывать ему нельзя, это сломало бы идемпотентность всех
  остальных маршрутов.

Единственная точка, где ещё можно решить, — третья строка `_invoke`.

### Что сделано в `apikeys.py`

* `IssuedKey.as_json()` возвращает `OneShotBody` — обычный `dict` (сериализуется,
  сравнивается, редактируется как раньше), который **помечает себя**: в нём есть
  поле `secret`, и он говорит, что это значение показывается один раз.
* `apikeys.carries_one_shot(result)` — «этот ответ можно ли хранить и повторять?»
  Истинно **только** для `OneShotBody`. Простой dict с полем `secret` — не
  одноразовый: экспорт с `include_secrets` скачивается повторно по
  `artifact_id`, и не надо молча менять поведение этого маршрута.
* `apikeys.without_one_shot(body)` — копия без `secret` и с флагом
  `secret_already_shown: true`. Молчаливый ответ без поля клиент прочтёт как
  «у этого ключа нет секрета»; явный флаг говорит «секрет уже выдан, крути».

### Точная правка

`proxy_workbench/apiv1.py`, `_invoke`, строка с `self.idempotency.put(...)`:

```python
-            self.idempotency.put(bucket, idem_key, digest, response)
+            self.idempotency.put(bucket, idem_key, digest, self._cacheable(result, response))
```

и рядом, одним методом (нужен `replace` из `dataclasses` и `apikeys` из
`proxy_workbench`; `apiv1.py` уже импортирует `dataclass, field`):

```python
    def _cacheable(self, result, response):
        """Никогда не хранить ответ, который несёт показываемый один раз секрет.

        `POST /v1/keys` и `POST /v1/keys/{id}/rotate` кладут полный секрет в тело
        (CONTRACTS §5.1). Если такой ответ попадёт в кэш идемпотентности, тот же
        секрет вернётся при каждом повторе запроса — вторая выдача, которую контракт
        запрещает. Поэтому в кэш уходит копия без секрета; маркер живёт на объекте
        ответа, а не на его JSON, поэтому проверять надо `result`, а не `body`.
        """
        if not apikeys.carries_one_shot(result):
            return response
        return replace(response, body=json.dumps(
            apikeys.without_one_shot(response.json()), ensure_ascii=False).encode('utf-8'))
```

**Проверено вживую** (патч применён в памяти, `apiv1.py` на диске не тронут —
скрипт `/tmp/pwk_repro/check.py`, пункт `1b`):

```
[OK   ] 1b the same replay WITH the apiv1 handoff patch applied in memory
       first  -> 200 secret=pwk_0526b2e4a5aa... id=8d1dfcaa39899a06
       replay -> 200 secret=absent secret_already_shown=True id=8d1dfcaa39899a06
       -> SECRET TWICE: False
```

Идемпотентность сохранена: тот же `id`, тот же статус, тот же ключ в базе.
Первый ответ по-прежнему отдаёт секрет — ровно один раз.

Если предпочтительнее честный отказ вместо ответа без поля: в `IdempotencyStore.get`
(строка `cached = self.idempotency.get(bucket, idem_key, digest)`, ~2003) для
записи с `body['secret_already_shown']` возвращать
`ApiError('E_CONFLICT_IDEMPOTENCY', status=409, ...)` с текстом «секрет уже
выдан, повтор его не вернёт». Это тоже корректно, но требует правки второго места;
вариант выше меняет одну строку.

### Смежно (желательно, но не обязательно)

`ApiKeyStore.create_key`/`rotate_key` в `apiv1.py` (~571 и ~591) возвращают
`issued.as_json()`. `POST /v1/subscriptions` идёт тем же путём и несёт тот же
секрет, поэтому правка `_invoke` закрывает оба маршрута сразу — отдельно ничего
трогать не нужно.

---

## 2. `apikeys.py` — очистка окна ротации (закрыто здесь)

`purge_expired_grace` существовал, но его звал только тест. Теперь вызывается из
рабочего пути:

* `ApiKeyManager.authenticate` — на каждый запрос (единственный путь, по которому
  живой сервер идёт всегда);
* `ApiKeyManager.assert_active` — для долгоживущих lease/stream, которые
  аутентифицируются один раз;
* `ApiKeyManager.rotate` — перед выдачей нового секрета;
* `ApiKeyManager.revoke` — при отзыве.

Внутренний `_sweep_grace` дросселирован (`GRACE_SWEEP_INTERVAL_S = 60`,
настраивается `grace_sweep_interval_s=`), молча выходит на базе без
аддитивных колонок и ловит `sqlite3.Error`. Явный `purge_expired_grace()`
по-прежнему выполняется всегда — этого требует `tests/test_apikeys_schema.py:62`.

Проверено вживую: после `rotate(grace_s=60)` + час + один `authenticate()` →
`previous_verifier` пуст, `rotation_grace_until` пуст, старый секрет не
проходит, новый работает.

---

## 3. `db.rebind_secrets` поднимает ревизию — два теста фиксируют старый контракт

`rebind_secrets` теперь двигает строку как `secrets.Coordinator.rotate`:
`access_revision + 1` и `rotated_at`. UPDATE оптимистичен по `access_revision`;
если строку переписали между чтением и записью — откат и отказ, а не «новая
ссылка на старой ревизии». `RebindReport` получил поле `revisions`
(`((access_id, from, to, rotated_at), ...)`), его печатает `proxytool backup
rebind` — оператор видит, на какую ревизию встал каждый доступ.

Проверено вживую:

```
before: access_revision=1 -> core.admit admitted=True
after : secret_ref='vault-ref-NEW', access_revision=2, rotated_at=1700000200.0
old evidence -> core.admit admitted=False (E_CONFLICT_ACCESS_REVISION)
```

### Что нужно владельцу тестов

1. **`tests/test_db_secrets.py:70-72`** — `test_rebind_writes_the_new_reference_and_nothing_else`
   утверждает `access_revision == 1` после перепривязки, с комментарием
   «no other column moved». Это и есть дефект: тест закрепляет поведение,
   которое требование запрещает. Ожидание должно быть `2`, а комментарий —
   «no other column moved» заменить на «the reference moved and the revision
   followed it».

2. **`tests/test_secrets_access.py:186-194`** —
   `test_a_rebind_to_a_matching_revision_keeps_working` кладёт замену в хранилище
   с `revision=self.access.access_revision` (то есть **на старой** ревизии) и
   ждёт, что `resolve()` сразу заработает. Теперь он честно отказывает:
   `secret store holds revision 1 but the database says 2`. Тест надо переписать
   так: замена готовится на `revision=self.access.access_revision + 1` —
   тогда `resolve()` работает, и `reconcile()` по-прежнему убирает осиротевшую
   ссылку. Второй тест класса
   (`..._to_a_foreign_revision_is_refused_until_reconciled`) остаётся верным.

### Что нужно владельцу `secrets.py`

Сообщение `SecretConflictError` в `resolve` (`secrets.py:1001-1004`) говорит
«run reconcile() before using this access». **`reconcile()` это не чинит.** В
`reconcile` ветка `state != STATE_READY` — то есть запись в состоянии `ready` с
чужой ревизией не проверяется вообще и молча остаётся. После перепривязки
нужно либо пере-stage записи на новой ревизии, либо научить `reconcile()`
выравнивать `ready`-запись, у которой ревизия отстала. Второе — решение
владельца `secrets.py`; до него отказ `E_CONFLICT` с корректным, но неверным
словом в `action` — единственная реакция.

Это осознанный размен: доступ, который нельзя разрешить, виден как сломанный;
доступ, который разрешается по отставшей ревизии, продолжает тихо доверять
измерениям, сделанным паролем, который пользователь уже заменил.

---

## 4. Retention: preview и apply больше не расходятся (закрыто здесь)

`_retention_targets` теперь считает **один исполнимый план**, и его читают и
`retention_preview`, и `apply_retention`:

* порядок удаления — из живых внешних ключей (`_retention_order`), а не из
  ручного списка; на стоковой политике `results` идёт раньше `observations`;
* блокеры считаются против таблиц, которые план **уже запланировал**, а не
  против таблиц, которые фактически что-то удалили. Раньше политика
  `include=("results",)` с пустым `results` и непустыми `observations` давала
  ложный отказ;
* заблокированные строки в `targets` считаются **нулём** — число, которое
  сообщает preview, это число, которое удаляет apply — и уходят в
  `preview.blocked` вместе с блокерами и настоящим количеством;
* `apply_retention` отказывает по `preview.blocked`, то есть по тому же
  посчитанному факту, а не по отдельному обходу.

`RetentionPreview` получил `blocked`, `blocked_by()`, `blocked_rows()`,
`runnable()`; в `to_dict()` — `runnable`, `blocked`, `blocked_rows`.

Проверено вживую на трёх политиках: стоковой (удаляет `results:1`, как и
обещал), `include=("observations",)` (preview пишет `observations:0`,
`blocked={observations: (results,)}`, `runnable=False`; apply отказывает с
названием блокера и лекарства), и `include=("results",)` с пустым ребёнком
(ложного отказа больше нет).

---

## 5. Промежуточная база не обновлялась вообще (закрыто здесь)

Отчёт описывал «миграция без копии». На деле было хуже: **любая база с
`user_version` 1..12 не обновлялась в принципе.**

`_m13` заполняет `results.endpoint_id` одним множественным выражением
`endpoint_id(proxy)`, а SQL-функция `endpoint_id` регистрировалась **только** в
`_m0`. База промежуточной сборки стартует выше миграции 0 и `_m0` не выполняет
никогда, поэтому `migrate()` падал:

```
E_DATA_MIGRATION_FAILED: migration 13 (results_rebuild) failed:
no such function: endpoint_id
```

Резервная копия тут ни при чём: до неё дело не доходило. Исправлено в
`db.connect()` — функция регистрируется на каждом соединении; регистрация в
`_m0` оставлена (она нужна, если миграции применяют на чужом `sqlite3.connect`).

Про саму копию: `DESTRUCTIVE_MIGRATIONS = {13, 15}` и условие
`pending & DESTRUCTIVE_MIGRATIONS` в `migrate()` уже были в дереве (коммит
`01faf77`) и корректны — я это проверил, а не переписывал. Проверено вживую на
всех промежуточных версиях: v1…v14 доходят до v15, и когда среди отложенных
был неаддитивный шаг, копия с manifest и совпадающим sha256 снималась до
миграции.

---

## 6. Замечание для всех: рабочее дерево меняется не только моими файлами

Пока велась эта работа, в `git status` появились правки в `api.py`,
`apiv1.py`, `desktop.py`, `gui.py`, `pipeline.py`, `proxytool.py`,
`sourcedesk.py`, `ui/*` — не мои. В момент проверки `proxytool.py:2046` не
парсился (`SyntaxError: expected 'except' or 'finally' block`), и
`tests/test_web_connect.py` падал из-за этого, а не из-за `db.py`/`apikeys.py`
(с откатом моих двух файлов он проходит: 24 теста, OK).

Полный прогон (`python -m unittest discover -s tests -t tests`) дал 2437 тестов.
Из них падают только два, и оба — следствие требуемого починки дефекта 3:
`tests/test_db_secrets.py:70` и `tests/test_secrets_access.py:192` (раздел 3).
Всё остальное проходит. Прогон по модулям, которых касаются мои файлы
(db/apikeys/apiv1/core/secrets): 364 теста, те же два падения.
