# Handoff: pools

**Требования:** F14 (постоянный пул), дефект 12 (watch/refill восстанавливает пул), R07 (watch только уменьшает)
**Контракт:** `docs/integration/CONTRACTS.ru.md` §1.2(5), §2.3, §3.3 (миграция 7), §4.3, §5.4 (версия 1)
**База:** ветка `integration/ultra-2026-09-25`, `db.py` (SCHEMA_VERSION 14) и `core.py` уже в дереве
**Мои файлы:** `proxy_workbench/pools.py`, `tests/pools_support.py`, `tests/test_pools_store.py`,
`tests/test_pools_lifecycle.py`, `tests/test_pools_quotas.py`

---

## 1. Прошу внести в чужие файлы

### 1.1 `proxy_workbench/proxytool.py`: watch-цикл вместо `recheck_passing=True`

- **Где:** `run_scan` и цикл `while args.watch:` (`proxytool.py:2500`, `recheck_passing=True` на
  `proxytool.py:2506`), сегодня он только перепроверяет прошедшие и снова публикует.
- **Что заменить:** после `export_now()` в watch-цикле вызвать контроллер пула вместо повторного
  `recheck_passing`-скана и передавать `want`:

  ```python
  from proxy_workbench import db
  from proxy_workbench import pools as poolctl

  store = poolctl.PoolStore.open(args.data, migrate=db.migrate)   # мигратор db, не DDL
  spec = store.get(args.pool) or store.create(
      args.pool, collection_id=..., profile_id=profile, profile_revision=...,
      desired=args.want, minimum=..., reserve=...,
      policy={'cooldown_seconds': 300, 'refill_budget': args.refill_budget})
  poolctl.watch(store, spec.id, source=candidates_from_engine, ticks=None,  # None = до остановки
                verify=measure_one_endpoint, clock=poolctl.Clock())
  ```

  `source(spec, kind, budget, now)` — это **единственный** новый вход, который движок должен
  реализовать: три уровня `('reserve', 'known', 'sources')`, каждый возвращает `Sequence[pools.Candidate]`
  и обязан уважать переданный `budget`. `verify(spec, member) -> True | False | None` — переизмерение
  участника в probation; `None` означает «измерения не было», и участник остаётся вне обслуживания.
  `ticks=None` — фоновый прогон до прерывания процесса, `on_status` может его остановить.
- **Почему здесь:** дефект 12 и R07 — именно этот цикл. `recheck_passing` по построению не может
  вернуть в пул отвалившийся адрес: он их не перебирает.

### 1.2 `proxy_workbench/api.py` (или `apiv1.py`): `pools.read` / `pools.write`

- **Где:** `public_row` / `select` (`api.py:51-89`, `:229-246`) — там нет ни пула, ни `deficit_reason`.
- **Что добавить:** два маршрута, отдающие `PoolStatus.as_dict()`:
  `GET /v1/pools` → список, `GET /v1/pools/{pool_id}` → `PoolStore.status(pool_id)`;
  `POST /v1/pools` (право `pools.write`) → `PoolStore.create(...)` с теми же кодами ошибок.
- **Почему:** CONTRACTS §4.3 требует `desired`, `deficit_reasons[]`, `next_attempt_at` в статусе,
  а `pool_id` — обязательный компонент resource scope (§5.3). `PoolStatus.as_dict()` уже содержит
  всё это и не содержит секретов.

### 1.3 `proxy_workbench/gui.py` + `ui/*` (поверхность `web`)

Ничего не прошу переписывать. Нужны только четыре поля в существующей карточке пула
(по именам, не по номерам строк): `served/desired`, `state`, `deficit_reason` + разбивка по кодам,
`next_attempt_at`. Файл `ui/*` мне не принадлежит, поэтому текст сюда и не пишу.

### 1.4 `CONTRACTS.ru.md` §5.4: домен `POOL`

- **Что:** в таблице кодов нет домена для причин недобора, а `pools.deficit_reason` (миграция 7) —
  это TEXT, и писать в него надо машинный код. Прошу добавить в §5.4 строкой:

  | Домен | Коды | HTTP |
  | --- | --- | --- |
  | `POOL` | `E_POOL_NO_CANDIDATES`, `E_POOL_COOLDOWN`, `E_POOL_SOURCE_ERROR`, `E_POOL_AT_CAPACITY`, `E_POOL_PUBLIC_IN_PRIVATE`, `E_POOL_SCOPE_...` (нет — scope уже есть в core), `E_POOL_QUOTA_{COUNTRY,PROTOCOL,ASN,EXIT_IP}`, `E_POOL_QUOTA_UNKNOWN_{...}`, `E_POOL_UNKNOWN_COLLECTION`, `E_POOL_UNKNOWN_MEMBER`, `E_POOL_DENIED`, `E_POOL_UNKNOWN` | 409 / 429 для `E_LIMIT_BUDGET` |

- **Уже существующие коды я переиспользую, а не дублирую:** `E_SCOPE_COLLECTION`, `E_SCOPE_COUNTRY`,
  `E_TIME_UNKNOWN`, `E_TIME_FUTURE`, `E_TIME_TTL_EXPIRED`, `E_TIME_TTL_MISSING` — все они уже есть в
  `core.REASON_CODES` (`proxy_workbench/core.py:108-134`), и тест
  `tests.test_pools_store.VocabularyTest.test_the_time_and_scope_codes_are_the_ones_core_publishes`
  следит, чтобы это не разъехалось. Если кандидат отобран по `core.admit`, его собственный код
  (`admission_reason`) попадает в разбивку недобора как есть.

### 1.5 `db.py`: необязательная колонка (не блокер)

`pool_member` (§3.3 миграция 7) не имеет счётчика отказов, поэтому нарастающего backoff для
повторно падающего адреса нет: окно cooldown одно и то же, повторную попытку отсекает probation.
Если нужен растущий backoff — нужна колонка `failures INTEGER NOT NULL DEFAULT 0` в `pool_member`
(целевая миграция 15, `db.py`). **Сейчас работает и без неё**, поэтому это запрос, а не блокер.

Второе необязательное: `deficit_reasons[]` целиком в `pools` не помещается (одна колонка
`deficit_reason TEXT`). Сейчас полная разбивка возвращается из `refill()`, а чтение через
`PoolStore.status()` показывает только основной код. Если разбивка нужна и на чтении — колонка
`deficit_reasons_json TEXT` (миграция 15).

---

## 2. Что уже сделано у меня: публичный API

```python
from proxy_workbench import pools

store = pools.PoolStore.open(path, migrate=db.migrate)   # migrate вызывается с ПУТЁМ
spec  = store.create('main', collection_id='public-base', profile_id='p1', profile_revision=1,
                     desired=5, minimum=3, reserve=2,
                     policy={'countries': ['DE'], 'quota': {'exit_ip': 1},
                             'cooldown_seconds': 300, 'refill_budget': 10})
store.get / store.list / store.require / store.set_target / store.set_policy
store.members(pool_id) / store.member(pool_id, endpoint_id) / store.status(pool_id)
store.add_member / store.set_member_state / store.remove_member / store.save_status

status = pools.refill(store, 'main', source, now=..., budget=..., verify=...)
statuses = pools.watch(store, 'main', source, ticks=5, verify=..., clock=..., on_status=...)
statuses_by_id = pools.refill_all(store, source, budget=...)
result = pools.report_health(store, 'main', 'ep-01', ok=False, now=..., reason=...)
result = pools.evict(store, 'main', 'ep-01', reason='E_SCOPE_DENYLIST')
```

Ключевые решения, на которые опираться:

1. **`refill()` — явная команда и всегда действует.** Частотный предохранитель живёт в `watch()`:
   тик, пришедший раньше `next_attempt_at`, возвращает статус с `deferred=True` и ничего не делает.
2. **Одна транзакция на refill.** Кандидаты собираются до первой записи; ошибка при записи
   откатывает весь refill (проверено тестом краша). `store.open()` открывает своё соединение, а
   `pools.transaction(conn)` работает и на `sqlite3.connect`, и на `db.connect` (`isolation_level=None`).
3. **Никакой второй admission.** `Candidate.allowed` / `admission_reason` приходят из `core.admit`;
   пул проверяет только те два времени (`valid_until`, `checked_at`), которые у него есть.
4. **Probation не обслуживает.** Участник в probation не входит в `served`, пока `verify` не вернул
   `True`; без `verify` он остаётся вне обслуживания и виден в `status.recheck_due`. Это и есть
   требование «не обслуживать непроверенное», а не «отбросить навсегда».
5. **Пустой пул никогда не «готов».** `ready_for_clients = served > 0 and served >= minimum`,
   поэтому при `minimum=0` пустой пул всё равно не готов — скрытого DIRECT не возникает.
6. **Неизвестное не считается значением.** `quota_unknown[dim]` = сколько участников пул не может
   описать; `quota_conflicts` = уже существующие превышения квоты, о которых пул знает.

## 3. Совместимость

- **Ничего не ломается:** новый файл, чужие таблицы только читаются и пишутся по именам колонок,
  `db.migrate()` не меняется, существующие тесты не трогаются.
- **Ломается, если не внести §1.1:** дефект 12 и R07 остаются открытыми — пул существует, но
  watch-цикл его не ведёт.
- **Ломается, если не внести §1.2:** у пула нет поверхности в API; F14 в части «показывает
  количество/причину/следующую попытку» закрыт только на уровне модуля.
- **Риск при интеграции:** пул не обслуживает доказательства старше `max_age_seconds`. Если
  интегратор не будет вызывать `report_health` или не передаст `verify`, пул честно опустеет через
  `max_age_seconds` и начнёт показывать `recheck_due`. Это осознанный выбор (просроченное доказательство
  не доказательство), но о нём надо знать при подключении.

## 4. Проверки

Команды, запущенные в этой сессии, из корня репозитория:

```
.venv/bin/python -m unittest tests.test_pools_store      → Ran 23 tests … OK
.venv/bin/python -m unittest tests.test_pools_lifecycle  → Ran 25 tests … OK
.venv/bin/python -m unittest tests.test_pools_quotas     → Ran 26 tests … OK
```

Итого 74 теста, все зелёные. Покрыто: приёмка F14 (5 → два отказа → 5; отсутствие резерва → честные
3/5; `budget=0` останавливает refill, включая promotion из резерва; краш сохраняет целевое состояние
и следующий refill его завершает), порядок уровней, жёсткие бюджеты `refill_budget`/`scan_limit`,
cooldown → probation → re-admission, истечение доказательства, усадка и расширение цели,
watch с тиками и без них, несколько именованных пулов и общий бюджет `refill_all`, квоты
страна/протокол/ASN/уникальный exit-IP с честным unknown, неизменяемость фильтра страны,
public-в-private, изоляция коллекций, отказ при пропавшей коллекции, схема и её отсутствие,
валидация цели и политики, `as_dict()` в JSON, отсутствие canary-секрета в файле БД и в `policy_json`.

Не прогонялось: полный `unittest discover -s tests` (по условию задачи его запускает интегратор),
`packaging/smoke.py`, сборка, GUI. Сетевых обращений тесты не делают: адреса из RFC 5737/RFC 6598,
источник и измерение — локальные функции.

## 5. Открытые вопросы

1. **Кто владелец `refill_budget` в CLI/GUI?** Я вынес его в политику пула (`refill_budget`, по
   умолчанию 10) и в аргумент `refill(budget=...)` для одного вызова. Нужен один флаг, а не два
   конкурирующих: предлагаю `--refill-budget N` у существующего scan-пути, значение по умолчанию —
   из политики пула.
2. **Ранг при усадке.** Когда `desired` уменьшают, лишние участники уходят в резерв в порядке
   `endpoint_id` — детерминированно, но не по качеству. Если нужен порядок по `score`, нужна
   колонка ранга в `pool_member` (или чтение из `endpoints`); сейчас этого нет.
3. **`probation_seconds` как порог, а не как лимит.** Участник, который дольше `probation_seconds`
   не был измерен, попадает в `status.probation_overdue` и ждёт дальше — он не выбрасывается.
4. **`trusted_private`.** `COLLECTION_PRIVATE = ('private', 'trusted_private')`: для trusted-приватной
   коллекции правило «public discovery не входит» действует так же. Если для неё нужна другая
   destination policy (F04), это изменение политики, а не пула.
