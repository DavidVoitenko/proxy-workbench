# Handoff: area-jobs (F11, F12 частично, F14, F15, дефекты 6 и 12)

**Требования:** F11 (задания и восстановление), F12 (частично — три единицы N и find-N у пула), F14 (постоянный пул), F15 (расписания и уведомления), дефект 6, дефект 12. Сценарии §7 пп. 10, 13, 19.
**Мои файлы:** `proxy_workbench/jobs.py`, `proxy_workbench/pools.py`, `proxy_workbench/scheduler.py`, `tests/test_areajobs_jobs.py`, `tests/test_areajobs_pools.py`, `tests/test_areajobs_scheduler.py`.
**Чужие файлы не правились.** Всё, что нужно от них, — в разделе 1.

---

## 0. Итог одной строкой на пункт

| Пункт | Состояние | Чем доказано живым прогоном |
| --- | --- | --- |
| F11: персистентные job/item, зафиксированный scope и ревизия | **сделано** | прогон §2.1, §2.3 |
| F11: очередь, pause/resume/cancel/retry | **сделано** | прогон §2.1 |
| F11: checkpoints, crash recovery, сон | **сделано** | прогон §2.1 |
| F11: идемпотентные управляющие действия | **сломано, починено** | §2.1, дефект A |
| F11: structured progress и events | **сделано** | прогон §2.2 |
| F11: ограниченное число пишущих в БД исполнителей | **сделано** | прогон §2.2 |
| **Дефект 6, сценарий 1** (остановка не теряет observations) | **сделано** | прогон §2.1 |
| **Дефект 6, сценарий 2** (повтор не создаёт дубликат) | **сломано, починено** | §2.1, дефект A |
| **Дефект 6, сценарий 3** (resume не расширяет scope) | **сделано** | прогон §2.1 |
| F12: три единицы N у конвейера | **сделано** (не мой файл) | прогон §3.1 |
| F12: пул просит у конвейера нужную единицу | **не было, сделано** | §3.2, §6 |
| F14: desired/minimum/reserve, refill из трёх уровней | **сделано** | прогон §4.1 |
| F14: cooldown → probation → re-admission | **сделано** | прогон §4.2 |
| F14: **приёмка 1** N=5 → 2 отказа → 5 | **сделано** | прогон §4.1, числа |
| F14: **приёмка 2** нет резерва → честные 3/5 | **сделано** | прогон §4.1, числа |
| F14: **приёмка 3** budget=0 останавливает refill | **сделано** | прогон §4.1, числа |
| F14: **приёмка 4** после crash целевое состояно цело | **сделано** | прогон §4.1, числа |
| F14: квоты страна/протокол/ASN/exit-IP, честный unknown | **сделано** | прогон §4.3 |
| F14: пусто/degraded с числом, причиной, следующей попыткой | **сделано** | прогон §4.3 |
| F14: без скрытого direct, без ослабления страны, public ≠ private | **сделано** | прогон §4.3 |
| F14: несколько именованных пулов, per-pool лимиты | **сделано** | `test_areajobs_pools.NamedPoolsTest` |
| **Дефект 12** watch/refill восстанавливает пул | **сделано** | прогон §4.2, числа |
| F15: интервалы и окна с timezone | **сделано** | прогон §5.1 |
| F15: **DST** (переход, без сдвига и задвоения) | **сделано** | прогон §5.2, числа |
| F15: пауза/возобновление, quiet hours | **сделано** | прогон §5.1 |
| F15: request/byte/time budgets | **сделано** | прогон §5.3, числа |
| F15: no-catchup после сна | **сделано** | прогон §5.4, числа |
| F15: metered/battery там, где ОС даёт сигнал | **сделано** | прогон §5.5 |
| F15: уведомления с dedup и hysteresis | **сделано** | прогон §5.6, числа |
| F15: probe/source/retry/judge/speedtest отдельно от relay | **сделано** | прогон §5.3, числа |
| F15: in-flight остаток конечен и объясним | **сделано** | прогон §5.3, числа |
| F15: наружу без конфигурации ничего не уходит | **сделано** | прогон §5.6, числа |
| F15: пауза и дневной бюджет переживают перезапуск | **сломано, чужой файл** | §1.1, §5.7 |

Три помечены «сломано, чужой файл», одно «не было, сделано». Всё остальное работает и проверено прогоном, не чтением кода.

---

## 1. Прошу внести в чужие файлы

### 1.1 `proxy_workbench/db.py` — миграция 7 (владелец `db.py`)

**Это единственный реальный блокер в моей области.** `schedules` в том виде, в каком его создаёт `db.migrate()`, содержит `id, pool_id, kind, interval_minutes, window_json, timezone, quiet_hours_json, budgets_json, next_run_at, enabled`. Нет `paused`, `pause_reason` и `counters_json`.

Живой прогон на реальной схеме (`/tmp` → `db.migrate()` → `Scheduler` → restart):

```
колонки schedules после db.migrate(): ['id', 'pool_id', 'kind', 'interval_minutes',
  'window_json', 'timezone', 'quiet_hours_json', 'budgets_json', 'next_run_at', 'enabled']
persists_runtime_state=False  persists_counters=False

ДО  restart: last_run_at=1777888800.0 paused=True counters=3/3
ПОСЛЕ restart: last_run_at=1777888800.0 paused=False pause_reason=None counters_requests=0
  -> ПАУЗА ПОТЕРЯНА
  -> ДНЕВНОЙ БЮДЖЕТ СБРОШЕН (0 вместо 3 из 3)
```

То есть **пользовательский pause исчезает после перезапуска**, и **дневной лимит запросов восстанавливается целиком**. Второе — прямое нарушение F15: лимит «работает», пока процесс жив, и перестаёт работать после перезапуска.

Прошу добавить в **миграцию 7** (не в новую — таблица уже создаётся):

```sql
-- schedules
paused            INTEGER NOT NULL DEFAULT 0,
pause_reason      TEXT,     -- paused_user | paused_quiet_hours | paused_battery | paused_metered
resume_at         REAL,
counters_json     TEXT,     -- {"requests":N,"bytes":N,"seconds":F,"reserved_*":N,"active":N,
                              --  "relay_requests":N,"relay_bytes":N,"reset_key":"...","reset_at":F}
```

`last_run_at` добавлять **не нужно**: конвейер уже восстанавливает якорь интервальной сетки из последней строки `schedule_run`, и это проверено тестом
`tests/test_areajobs_scheduler.SqliteStoreTest::test_the_last_run_survives_as_the_anchor_of_the_interval_grid`.
`dst_policy`, `catch_up`, `max_catch_up`, `wake_gap_s`, `power_json`, `notify_json`, `notifications_json` — необязательны для заявленного F15, но без них настройки DST/catch-up/power не переживают перезапуск. Полный список остаётся в `scheduler.REQUESTED_COLUMNS`.

**Что я сделал, пока миграции нет:** `Scheduler.report()` теперь отдаёт секцию `persistence` с честным списком `lost_on_restart` (см. §5.7). Молчаливый сброс бюджета — хуже, чем его отсутствие: пользователь смотрит на счётчик и доверяет ему. Теперь интерфейс может сказать «лимит не переживает перезапуск», вместо того чтобы показывать работающее ограничение, которого нет.

### 1.2 `proxy_workbench/api.py` — `_op_pools_recheck` и `_op_pools_refill`

**`_op_pools_recheck` (`api.py:1805-1832`) ничем не исполняется.** Маршрут создаёт job вида `pool_recheck` с items и возвращает `job_id`, но в дереве нет ни одного потребителя этого `kind`: `grep -rn "pool_recheck" proxy_workbench/` даёт только создание в `api.py` и в `gui.py`. Клиент получает `job_id` задания, которое никто никогда не выполнит.

Прошу: либо (а) `proxytool` забирает задания этого вида и прогоняет их через `scan(...)` с профилем и ревизией пула, либо (б) маршрут перестаёт обещать job и делает то, что уже делает `_op_pools_refill` — синхронный refill. Вариант (а) правильнее: он сохраняет наблюдения, прогресс и восстановление, ради которых F11 и написан.

**`pool_candidate_source` (`api.py:2589-2640`) не отдаёт `exit_ip`, `country` и `asn`.** Кандидат создаётся только с `endpoint_id`, `canonical`, `collection_id`, `allowed`, `admission_reason`, `checked_at`, `valid_until`, `protocol`, `origin_domain`. Следствия, которые я проверил живьём:

* квота `{'exit_ip': 1}` на этом источнике **не может быть выполнена в принципе** — все кандидаты имеют `exit_ip=None`; при `quota_unknown: ignore` пул примет их и покажет `quota_unknown={'exit_ip': N}`, при `reject` — отвергнет всех и останется пустым;
* квоты `country` и `asn` не работают по той же причине;
* `find_request()` пула вернёт `what='exit'` (квота есть), и конвейер будет искать подтверждённые выходы, которых источник не предоставляет.

Прошу читать из `results.payload` (там `anonymity.exit_ip`, `geo.country`, `geo.asn` — см. `proxytool.exit_address_of` и `core`) и передавать в `Candidate`. Это единственное место в дереве, где пул получает кандидатов для F14, и без этих трёх полей квоты F14 на живом API-пути декоративны.

### 1.3 `proxy_workbench/proxytool.py` — точка подключения пула к конвейеру

Раньше handoff просил пул звать `pipeline.FindPolicy(n=pool.desired, what='exit')`. **Я это сознательно не сделал** — обоснование в §6. Кратко: единица, в которой пул считает `desired`, выводится из его же политики и едет в `PoolStatus.find` / `PoolStatus.count_unit`. Интегратору остаётся одно:

```python
from . import pipeline as chain
status = workbench.pools().status(pool_id)          # уже содержит find и count_unit
scan(..., want=status.find['n'], count_what=status.find['what'])
# или то же самое без dict:
find = status.find_policy(chain.FindPolicy)          # chain.FindPolicy(n=..., what=...)
```

`pools` не импортирует `pipeline` — модуль остаётся leaf, а несовпадение единиц проявится `ValidationError` в точке вызова, а не тихим дефолтом.

### 1.4 `proxy_workbench/gui.py` — `pool_status` считает `served` неправильно

`gui.py:3305`:

```python
served = sum((status.counts or {}).values())
```

Это сумма **всех** фаз: `active + reserve + probation + cooldown`. Пул с 3 активными, 2 в резерве и 2 в cooldown покажет `served=7` при `desired=3`. У `PoolStatus` уже есть правильное `served` (только `active`) и `count_unit`; прошу использовать их и показать `count_unit` рядом с числом — иначе «5» означает разные вещи в разных пулах.

### 1.5 `proxy_workbench/proxytool.py` — `--pool-refill` / `--count-what` в CLI

`_cmd_pool` (`proxytool.py:4196-4253`) умеет `list|create|status|members`, но не `refill` и не `recheck`, хотя `pools.refill` и `pools.watch` готовы и используются API. Разрыв между CLI и API в этой области — тот же класс проблемы, что F18/F29 требуют закрыть. Прошу два подкоманды с теми же кодами ошибок, что у `/v1/pools/{id}/refill`.

---

## 2. F11 — живые прогоны

### 2.1 Дефект 6, три сценария (`.venv/bin/python /tmp/areajobs/f11_defect6.py`)

**Сценарий 1 — остановка на любой стадии не теряет завершённые observations.**

```
ДО   : finished=2 unfinished=6 by_state={'pending': 5, 'probing': 1, 'done': 2}
       items с observation_id: ['ep-01', 'ep-02']
ПОСЛЕ cancel: state=cancelled finished=2 unfinished=6 by_state={'pending': 5, 'probing': 1, 'done': 2}
       items с observation_id: ['ep-01', 'ep-02']  -> потеряно: set()
ПАУЗА : до finished=3 -> после finished=3, state=paused, unfinished=5
```

Покрыто пятью точками остановки (`before-judgement`, `cancel`, `pause`, `deadline`, `crash`) в `test_areajobs_jobs.StopKeepsFinishedObservationsTest`. Отдельно: измерение без вердикта (`record_observation` без `finish_item`) переживает крэш — `test_a_crash_between_measuring_and_judging_keeps_the_measurement`.

**Сценарий 2 — повторный запрос не создаёт дубликат.** Это был **дефект A** (§7). После починки:

```
первый  job.id = job-81a7e87308ae63be
повтор  job.id = job-81a7e87308ae63be   тот же? True
строк в job: 2 -> 2;  job_item у job1: 8 -> 8;  событий: 8 -> 8
тот же ключ + другой ввод -> E_CONFLICT_IDEMPOTENCY: key 'req-1' is already bound to job job-81a7e87308ae63be
строк в job после конфликта: 2
```

Проверено во всех состояниях, до которых job может дойти: `queued`, `running`, `paused`, `cancelled`.

**Сценарий 3 — resume не расширяет scope и не использует obsolete membership.**

```
после resume: items всего=8  by_state={'pending': 4, 'done': 3, 'blocked': 1}
ep-04 вышел из коллекции -> state=blocked error_code=OBSOLETE_MEMBERSHIP
в очереди осталось: ['ep-05', 'ep-06', 'ep-07', 'ep-08']
resume с profile_revision=2 -> E_CONFLICT_REVISION: resume keeps the recorded scope; submit a new job instead
recover(): jobs=['job-270f697445b2c4da'] requeued=4 observations_kept=3
после recover: state=paused finished=4 unfinished=4 by_state={'pending': 4, 'done': 3, 'blocked': 1}
observation_id пережили: ['ep-01', 'ep-02', 'ep-03']
```

Коллекция выросла до 10 адресов между созданием job и resume — `resume` дал 8 items, не 10.

### 2.2 Ограниченное число пишущих и структурированный прогресс

```
второй start -> E_CONFLICT_BUSY job job-0f37f12bb0c9ba20 is running
второй WriterGate -> E_CONFLICT_BUSY the database is being written by scanner-1
второй процесс start -> E_CONFLICT_BUSY job job-0f37f12bb0c9ba20 is running

progress by_state = {'pending': 4, 'done': 6}   total/finished/unfinished = 10 6 4
событий = 15, seq = [1..15] без пропусков
типы = ['item.observation', 'item.verdict', 'job.state']
один item.verdict на item? True
item_id заполнен у item.* ? True
курсор after_seq=N-2 вернул [14, 15]
emit с password -> E_VALIDATION_FIELD data.password must not carry credentials
```

Сон — не авария: восемь часов сна при часовом бюджете дают `state='running'` и `remaining_s > 3500`; час реальной работы даёт `timed_out`.

---

## 3. F12 и единицы N

### 3.1 Три единицы у конвейера работают

`--count-what {endpoint,ip,exit}` → `chain.FindPolicy(n=want, what=count_what)` (`proxytool.py:1842`). Моё участие: пул теперь говорит на том же языке (§6).

### 3.2 Что пул просит у конвейера

```
обычный пул (желает 5 адресов)
   count_unit=endpoint  served=5/5  shortfall=0
   find={'n': 0, 'what': 'endpoint'}  -> FindPolicy: n=0 what='endpoint' counted=None
пул с разными exit-IP (quota exit_ip:1)
   count_unit=exit  served=5/5  shortfall=0
   find={'n': 0, 'what': 'exit'}  -> FindPolicy: n=0 what='exit' counted=None

пул просит 5 разных выходов, Supply есть 3: served=3/5 shortfall=2
   status.find = {'n': 2, 'what': 'exit'}
   pipeline.FindPolicy -> n=2 what='exit' counted='passed_exit_ips'
   deficit=E_POOL_NO_CANDIDATES state=degraded next_attempt_at=1000060.0
без квоты тот же пул просит единицу endpoint:
   served=3/5 shortfall=2 find={'n': 2, 'what': 'endpoint'}
```

---

## 4. F14 и дефект 12

### 4.1 Приёмка F14, буквально (`.venv/bin/python /tmp/areajobs/f14_acceptance.py`)

**N=5 → два отказа → восстановление до 5:**

```
после первого refill             served=5/5 active=5 reserve=2 cooldown=0 state=complete
отказ ep-01 -> cooldown next_try=1000310; ep-02 -> cooldown
сразу после двух отказов         served=3/5 active=3 reserve=2 cooldown=2 state=complete
refill (резерв поднимает)        served=5/5 active=5 reserve=1 cooldown=2 state=complete
  promotions=2  участники: {'ep-01': 'cooldown', 'ep-02': 'cooldown', 'ep-03': 'active',
                            'ep-04': 'active', 'ep-05': 'active', 'ep-06': 'active',
                            'ep-07': 'active', 'ep-08': 'reserve'}
ИТОГ F14-1: served=5/5 state=complete
```

**Отсутствие резерва → честные 3/5:**

```
после двух отказов, reserve=0    served=3/5 active=3 reserve=0 cooldown=2 state=degraded
                                  reason=E_POOL_COOLDOWN
desired=5 shortfall=2 below_minimum=False ready_for_clients=True
разбивка недобора: [('E_POOL_COOLDOWN', 2)]
next_attempt_at=1000078 (сейчас 1000018, retry_interval=60)
ИТОГ F14-2: served=3/5 state=degraded reason=E_POOL_COOLDOWN
```

**budget=0 останавливает refill:**

```
refill с budget=0     served=0/5 active=0 state=empty reason=E_LIMIT_BUDGET
источник спрошен: []   (пусто = за кандидатами даже не пошли)
budget_limit=0 budget_spent=0 admissions=0
ИТОГ F14-3a: served=0/5, state в БД = empty, участников = 0
...
ИТОГ F14-3b: budget=0, promotions=0, served=4/5, reason=E_LIMIT_BUDGET  (резерв НЕ поднят)
budget=1 -> promotions=1 served=5/5 state=complete
```

**После crash сохраняется целевое состояние:**

```
ДО    : desired=5 minimum=3 reserve=2 state=complete; served=5/5
        участники: {'ep-01': 'cooldown', 'ep-02': 'active', 'ep-03': 'active',
                    'ep-04': 'active', 'ep-05': 'active', 'ep-06': 'active',
                    'ep-07': 'reserve', 'ep-08': 'reserve'}
ПОСЛЕ : desired=5 minimum=3 reserve=2 state=complete; served=5/5
        участники те же: True
desired изменён на 7 до restart -> после restart desired=7
refill после restart             served=7/7 active=7 reserve=2 cooldown=1 state=complete
ИТОГ F14-4: served=7/7 state=complete
```

`st.close()` в середине — это и есть «процесса больше нет», без graceful shutdown.

### 4.2 Дефект 12 (`.venv/bin/python /tmp/areajobs/f14_defect12.py`)

```
тик 0: served=3/3  участники={'ep-01': 'active', 'ep-02': 'active', 'ep-03': 'active', 'ep-04': 'reserve'}
отказ ep-01: served=3/3  участники={'ep-01': 'cooldown', 'ep-02': 'active', 'ep-03': 'active',
                                   'ep-04': 'active', 'ep-05': 'reserve'}
отказ ep-02: served=3/3  участники={'ep-01': 'cooldown', 'ep-02': 'cooldown', ...}
после cooldown + успешного замера: served=3/3  участники={'ep-01': 'reserve', 'ep-02': 'probation',
                                   'ep-03': 'active', 'ep-04': 'active', 'ep-05': 'active'}
recheck_due=('ep-02',) re_admissions=1

--- watch() держит пул, а не выбрасывает ---
тик 0: served=2/3 state=complete reason=None deferred=True
тик 1: served=3/3 state=complete reason=None deferred=False
...
ep-01 всё ещё в таблице пула: True (state=active)
```

Адрес не исключается: `cooldown → probation → (новая measurements) → active|reserve`. Измерение, которое бросило исключение, не считается доказательством смерти — адрес остаётся в `probation` и в `recheck_due`.

### 4.3 Квоты, unknown, честный отказ (`.venv/bin/python /tmp/areajobs/f14_quotas.py`)

```
КВОТА exit-IP (quota exit_ip:1), 5 кандидатов, два с ОДНИМ exit, два без exit:
  served=3/3  участники={'ep-01': 'active', 'ep-03': 'active', 'ep-04': 'active'}
  quota_unknown = {'exit_ip': 1}   (сколько участников пул не может описать)
  deficit_reasons = [('E_POOL_AT_CAPACITY', 1), ('E_POOL_QUOTA_EXIT_IP', 1)]
  -- тот же набор, unknown exit отвергается (fail closed) --
  served=2/3  deficit_reasons = [('E_POOL_QUOTA_UNKNOWN_EXIT_IP', 2), ('E_POOL_QUOTA_EXIT_IP', 1)]

СТРАНА не ослабляется: фильтр countries=[NL], кандидаты DE/FR/unknown
  served=0/2 state=empty reason=E_SCOPE_COUNTRY  разбивка: [('E_SCOPE_COUNTRY', 3)]
  ready_for_clients=False  участники: []   (DE НЕ прошёл)

PUBLIC в PRIVATE: private-коллекция, кандидаты public + own
  served=1/1 reasons=[('E_POOL_PUBLIC_IN_PRIVATE', 1)]  участники: ['ep-02']  (public НЕ попал)
```

Квоты страны, протокола и ASN дают `served=3/3` при лимите 2 на значение, второй кандидат с тем же значением отвергается кодом `E_POOL_QUOTA_<DIMENSION>`.

---

## 5. F15 — живые прогоны

### 5.1 Окна, тихие часы, пауза/возобновление

```
после окна              18:30 -> skip window_closed resume_at=2026-05-05T09:00:00+02:00
в тихие часы            22:30 -> skip paused_quiet_hours resume_at=2026-05-05T07:00:00+02:00
в тихие часы            23:00 -> skip paused_quiet_hours   повторов: 0
конец тихих часов       07:30 -> skip window_closed   (пауза поднялась сама)
окно открылось          09:00 -> run  coalesced_no_catchup
уведомления: [('SCHEDULE_OUTSIDE_WINDOW', 'day'), ('SCHEDULE_QUIET_HOURS', 'day')]
```

`run_now` (ручной запуск) уважает ту же политику: 23:00 в тихие часы → `None`, 20:00 вне окна → `None`, на паузе → `None`. Ручной запуск не обход политики.

### 5.2 DST через границу перехода

Слот `02:30` Europe/Berlin, 2026-03-28…31 (29-е — день перехода, 02:30 не существует):

```
dst_policy=skip : 28: 2026-03-28T01:30Z | 29: — | 30: 2026-03-30T00:30Z | 31: 2026-03-31T00:30Z
dst_policy=shift: 28: 2026-03-28T01:30Z | 29: 2026-03-29T01:00Z | 30: ... | 31: ...
число непустых слотов за 28-30 марта (skip): 2   (28 и 30 — 29-е не существует, не задваивается)
```

**Задание не сдвинулось и не задвалось.** Интервальная сетка через ту же границу:

```
якорь last_run_at = 2026-03-28 12:00:00+01:00
tick в 2026-03-29 12:00:00+02:00: action=run reason=coalesced_no_catchup
                                    next_run_at=2026-03-29 13:00:00+02:00
elapsed = 23.0 hours   runs = 2   requests = 1
```

Сутки 28→29 марта — это 23 часа реального времени, и сетка считает именно их. Слот `02:30` в `America/Santiago` (переход в полночь): 2026-09-06 не существует, 07/08/09 — обычные моменты.

### 5.3 Бюджеты: лимиты, in-flight, relay отдельно

```
бюджет: requests limit=5 inflight_reserve_requests=1
отказ на попытке 7: code=E_LIMIT_BUDGET reason=requests_exhausted axis=requests
                    used=6 limit=5 in_flight=1 allowance=1.0
выдано probe-запросов: 6 из 5 лимита
snapshot requests={'limit': 5, 'used': 6, 'reserved': 0, 'remaining': 0.0,
                   'allowance': 1.0, 'over_limit': True, 'exhausted': True}
остаток in-flight конечен и объясним: in_flight=1 <= allowance=1.0: True

relay 100 req / 1 GB при исчерпанном рабочем бюджете: ok=True
рабочий бюджет не тронут: requests 6 -> 6; relay_requests=100, relay_bytes=1000000000
```

Пять классов рабочего трафика (`PROBE, SOURCE, RETRY, JUDGE, SPEEDTEST`) делят один бюджет и не имеют своего; `RELAY` считается отдельно и **никогда** не тратит рабочий лимит и не появляется в нём.

### 5.4 No-catchup после сна

```
после сна 3 часа при интервале 10 мин: run_requests=1 reason=coalesced_after_sleep
                                        missed=17 woke=True
```

Восемнадцать просроченных вхождений схлопнулись в один запуск, семнадцать **посчитаны** в `missed`, а не потеряны молча. Без `catch_up` — то же; с `catch_up: true, max_catch_up: 2` на шесть просроченных — ровно два запуска и `missed=4`. Сетка не дрейфует: двенадцать тиков подряд с интервалом 10 минут дают ровно двенадцать запусков.

### 5.5 Питание и трафик

```
сигнала нет          -> restricted=False reason=power_signal_absent   (политика НЕ применяется)
сигнал: батарея 20% + metered -> restricted=True reason=battery_restriction
metered: cheap_only  -> restricted=False stages=('probe','source','retry')
metered неизвестен, treat_as_metered   -> reason=metered_unknown_conservative
metered неизвестен, treat_as_unmetered -> reason=power_policy_off
```

### 5.6 Уведомления: dedup, hysteresis, и наружу — только по выбору пользователя

```
11 наблюдений подряд (10 -> 0), изменений состояния: 1
    healthy=1 -> ok->alert
всего отправлено уведомлений: 1   (десять одинаковых деградаций -> одно)
после восстановления до 6: ['BELOW_MINIMUM', 'RECOVERED']

пустая конфигурация             email отправлено=0 webhook=0 -> [('email','target_not_configured'), ...]
email есть, но не подтверждён   email отправлено=0 webhook=0 -> [('email','not_user_confirmed'), ...]
email подтверждён, но выключен  email отправлено=0 webhook=0 -> [('email','target_disabled'), ...]
email включён и подтверждён     email отправлено=1 webhook=0
```

Проверено перехватом канала, а не отсутствием исключения: `TrapChannel` получает `send()` только в последнем случае. Отдельный тест падает, если я добавлю в `scheduler.py` `import socket`, `smtplib`, `urllib.request` или `http.client`.

### 5.7 Что осталось неработающим в F15

Пауза и дневной бюджет не переживают перезапуск — §1.1, числа приведены там. Модуль теперь сообщает об этом в `Scheduler.report()['persistence']`:

```python
{'persists_runtime_state': False, 'persists_counters': False,
 'lost_on_restart': ['paused', 'counters'],
 'requested_columns': {'schedules': ['last_run_at', 'paused', ...], 'schedule_run': [...]}}
```

Интервальная сетка при этом **не теряется**: якорь восстанавливается из `schedule_run`, и повторный тик не запускает второй прогон.

---

## 6. Про `FindPolicy(n=pool.desired, what='exit')` — решение

**Проверено: не выполнено.** `grep -c "pipeline\|FindPolicy" proxy_workbench/pools.py` → `0`; единственный `FindPolicy` в движке строится в `proxytool.py:1842` из `--want/--count-what`, и пул в этот путь не входит.

**Сделал не так, и вот почему.** Требование «N подтверждённых выходов» и устройство пула — разные вещи, и подменить одно другим нельзя:

1. **Схема.** Участник пула — строка `pool_member(pool_id, endpoint_id, state, admitted_at, released_at)`. В ней нет ни exit-адреса, ни страны, ни ASN. Считать выходы по members нельзя *в принципе*: после перезапуска у pools нет источника этих значений, и любой такой счётчик обнулился бы. Единственное место, где exit известен, — `results.payload`, то есть измерение, а не membership.
2. **Приёмка F14 сформулирована в участниках.** «N=5 → два отказа → восстановление до 5» — это пять адресов в обслуживании. Пять адресов за одним выходом — это пять адресов, и pool-приёмка должна считать их пятью.
3. **Требование «разные выходы» уже выражено в владении пула.** `quota={'exit_ip': 1}` + `desired=5` — это ровно «пять прокси с разными выходами»: проверяется на каждом допущении, даёт `E_POOL_QUOTA_EXIT_IP`, умеет fail-closed через `quota_unknown`, и это уже доказано прогоном в §4.3. Это **прямое требование F14** («квоты … уникальному exit-IP»), в отличие от find-N, который F14 не упоминает.

Поэтому единица **выводится из политики**, а не зашивается: `quota.exit_ip` есть → `count_unit='exit'`, иначе `'endpoint'`. `what='ip'` исключён намеренно — «IP прокси» не то, что пул держит.

Что при этом сделано, чтобы расхождение единиц не могло стать тихим:

* `PoolSpec.count_unit` и `PoolSpec.find_request(served)` — производные от политики;
* `PoolStatus.count_unit` и `PoolStatus.find` едут в каждом статусе и в `as_dict()`, так что вызывающая сторона не угадывает;
* `PoolStatus.find_policy(chain.FindPolicy)` строит настоящий объект конвейера. `pools` **не импортирует** `pipeline`: если единица не совпадёт, это `ValidationError` в точке вызова, а не молчаливый дефолт;
* `pools.FIND_UNITS == chain.FIND_BY == ('endpoint','ip','exit')` — тест `test_the_three_units_are_the_same_vocabulary_the_cli_and_the_pipeline_use` следит за этим;
* `FIND_UNIT_FOR_DIMENSION` разделяет словари модулей: пул хранит `exit_ip`, конвейер считает `exit`. Первая версия отображения отдала движку `what='exit_ip'`, и `pipeline` её отверг — это был мой баг, пойманный живым прогоном и закрытый тестом `test_the_unit_it_asks_for_is_one_the_engine_accepts`.

**Что осталось за мной:** контракт `find`/`count_unit` в `PoolStatus` и `find_policy()` вместо жёсткого `what='exit'`. **Что за интегратором:** прокинуть `status.find` в `scan(..., want=, count_what=)` (§1.3) и научить `pool_candidate_source` отдавать `exit_ip`/`country`/`asn`, без которых квота `exit_ip` на живом API-пути не может быть выполнена (§1.2).

---

## 7. Найденные и починенные дефекты

### Дефект A. `jobs.submit` ломал идемпотентный повтор (F11, F29 приёмка №3) — **починено**

`submit` делал `queue(create_job(...))`. `create_job` при повторе с тем же ключом возвращает **существующий** job, а `queue` на нём пытался перевести `running`/`partial`/`cancelled` в `queued` — то есть в состояние, из которого этот переход запрещён.

Живой прогон **до** починки:

```
first  : job-3f752010fb313eab queued
running: running
replay while running -> RAISES E_STATE_JOB_TRANSITION: running -> queued is not a job transition
succeeded: partial
replay after finish -> RAISES E_STATE_JOB_TRANSITION: partial -> queued is not a job transition
rows in job table: 1
```

Второй job не создавался — то есть «дубликата нет» формально выполнялось, а ответ клиенту был ошибкой вместо того же `job_id`. Для F29 приёмки №3 («один idempotency key не запускает второй scan») это всё равно неверно: клиент, не получивший ответ и повторивший запрос, получал 500/409 вместо своего же `job_id`. Починка — `submit` возвращает существующий job как есть, если тот не в `created`; состояние job при этом не меняется, новых событий нет. Регрессия закрыта `test_a_replay_returns_the_same_job_in_every_state_it_can_reach`, `test_a_replay_does_not_add_a_single_event`.

### Дефект B. `pools.refill` держал write-lock во время измерения (§6.4) — **починено**

`verify` — это сеть вызывающей стороны, а он выполнялся **внутри** `transaction(store.conn)`. Живой прогон: другой writer получает `database is locked` на всё время каждого измерения.

```
('enter', ...)
('other-writer', 'REFUSED: database is locked')   # ep-01
('other-writer', 'REFUSED: database is locked')   # ep-02
```

Починка: refill разбит на три шага — переходы фаз коммитятся сами по себе, измерения идут **без открытой транзакции**, вердикты и допуски садятся одной транзакцией. После починки тот же прогон:

```
ep-01: другой writer ПРОПУЩЕН (окно БД свободно)
ep-02: другой writer ПРОПУЩЕН (окно БД свободно)
served= 2 re_admissions= 2
```

Граница честно описана в докстринге `refill`: краш между первым коммитом и вторым **не** откатывает первый. Альтернатива — держать write-lock на время сетевого round-trip, что хуже. Что при этом гарантировано: ни один участник не теряется, цель пула сохраняется, следующий refill её завершает — проверено `test_a_crash_between_the_phase_changes_and_the_admissions_leaves_a_pool_a_refill_finishes` (7 участников на месте, цель 5, следующий refill даёт `served=5`).

### Дефект C. Тихая потеря паузы и бюджета при перезапуске (F15) — **починено частично, остальное — чужой файл**

Числа в §1.1. Свою часть сделал: `Scheduler.report()` и новый `Scheduler.persistence()` теперь называют потерянное (`lost_on_restart`), вместо того чтобы показывать работающий лимит, которого нет. Само исправление — миграция в `db.py`, §1.1.

### Дефект D (НЕ починен — нужен владелец `tests/test_jobs_events.py`)

`JobStore.retry()` по умолчанию берёт всё, что не `done`, и **включает `blocked`** — то есть адреса, помеченные `OBSOLETE_MEMBERSHIP` при выходе из коллекции. Живой прогон:

```
после resume:  {'e1': ('done', None), 'e2': ('blocked', 'OBSOLETE_MEMBERSHIP'), 'e3': ('blocked', ...)}
retry создал job-f476589272f17521 с очередью: ['e2', 'e3']
  -> blocked-адреса ПЕРЕМЕРЕНЫ заново без проверки membership?
```

`resume` этот случай закрывает, `retry` — нет: тот же запрет на obsolete membership приходит через другую дверь. Починить не смог: тест `tests/test_jobs_events.py::HistoryTests::test_the_store_never_writes_outside_its_own_four_tables` вызывает `self.store.retry(job.id)` на job, у которого единственные незавершённые items — как раз `blocked`, и падает с `E_STATE_JOB_TRANSITION: nothing to retry`. Файл мне не принадлежит. Прошу владельца либо принять изменение `retry` (исключить `blocked` из выборки по умолчанию и внятно объяснять отказ), либо решить, что retry имеет право перемерить вне membership — тогда это нужно записать в контракт, чтобы следующий читатель не искал баг.

---

## 8. Проверки

Из корня репозитория, `.venv/bin/python`:

```
$ .venv/bin/python -m unittest tests.test_areajobs_jobs tests.test_areajobs_pools \
      tests.test_areajobs_scheduler
Ran 97 tests in 0.468s — OK

$ .venv/bin/python -m unittest tests.test_jobs_lifecycle tests.test_jobs_items \
      tests.test_jobs_recovery tests.test_jobs_events tests.test_pools_store \
      tests.test_pools_lifecycle tests.test_pools_quotas tests.test_pools_api \
      tests.test_scheduler_windows tests.test_scheduler_budgets \
      tests.test_scheduler_notifications tests.test_scheduler_plans tests.test_scheduler_store
Ran 286 tests in 1.739s — OK

$ оба набора вместе
Ran 383 tests in 2.024s — OK

$ .venv/bin/python -m unittest discover -s tests
Ran 3105 tests in 199.851s — FAILED (failures=3, errors=1, skipped=1)
```

Полный `discover` гонял и интегратор по условию задачи; сообщаю результат, чтобы падение не оказалось сюрпризом. Оставшиеся 4 — **не мои и существовали до моих правок**: 3 × `test_unmeasured_transports_are_declared_unsupported` (`tests/test_probes_reference.py`, владелец `probes.py`) и `test_config` (ошибка без действия). Контрольный прогон с моими тремя файлами, откатанными через `git stash`, даёт ровно те же 4 **плюс** 9 ошибок из моих новых тестов — то есть мои тесты падают ровно на тех дефектах, которые я починил, и не падают ни на чём постороннем:

```
=== BASELINE (три файла откачены) ===
   3 FAIL: test_unmeasured_transports_are_declared_unsupported
   1 ERROR: test_the_unit_it_asks_for_is_one_the_engine_accepts
   1 FAIL: test_another_writer_gets_in_while_the_pool_is_measuring
   1 ERROR: test_the_three_units_are_the_same_vocabulary_the_cli_and_the_pipeline_use
   1 ERROR: test_ip_is_never_the_unit_of_a_pool
   1 ERROR: test_config
   1 ERROR: test_a_replay_returns_the_same_job_in_every_state_it_can_reach
   1 ERROR: test_a_pool_that_caps_members_per_exit_asks_for_confirmed_exits
   1 ERROR: test_a_plain_pool_asks_for_endpoints
   1 ERROR: test_a_pause_and_a_spent_budget_are_reported_as_lost_when_the_schema_cannot_hold_them
   1 ERROR: test_a_full_pool_asks_for_nothing
```

**Сетевых обращений тесты не делают.** Адреса — из RFC 5737 (198.51.100.0/24) и RFC 6598; база — временный файл, созданный настоящим `db.migrate()`; часы внедряются; источник кандидатов и измерение — локальные функции. Ни одного живого публичного прокси в проверке нет, и ни одна цифра в этом документе не является утверждением о качестве живых адресов.

**Что осталось непроверенным:**

* путь `pool_recheck` (job → `scan`) не существует ни в одном файле, §1.2 — проверить нечего;
* квоты `country`/`asn`/`exit_ip` проверены на источнике, который их предоставляет; на живом `api.pool_candidate_source` они сейчас бессильны, §1.2;
* поведение под реальной нагрузкой и на реальном `pmset`/`/sys/class/power_supply` не измерялось: всё считано на подставных входах;
* `probes`, `api`, `gui` я не правил, поэтому интеграционные сквозные сценарии (GUI → пул → шлюз) не гонялись.

---

## 9. Открытые вопросы

1. **Единица пула — мой выбор, не требование.** §6 целиком. Если команда считает, что `desired` должен измеряться в выходах всегда, менять надо не `find`, а схему: колонка `exit_ip` в `pool_member` плюс решение, что делать с `unknown`. Я против «считать, но не сохранять» — это ровно тот счётчик, который обнулится при перезапуске.
2. **`_retire` по умолчанию 24 часа.** Адрес, который отдывает дольше суток без замера, забывается пулом. Это граница таблицы, а не запрет: адрес остаётся в коллекции, и любой уровень источника может его предложить снова. Нужно ли это пользователю настраивать — вопрос к GUI, поле уже есть (`retire_seconds`).
3. **Ранг при усадке.** Уменьшение `desired` отправляет лишних в резерв по `endpoint_id` — детерминированно, но не по качеству. Для ранга по score нужна колонка в `pool_member`; сейчас её нет, и я бы не просил её без отдельного требования от владельца продукта.
4. **Дефект D (§7) ждёт владельца `tests/test_jobs_events.py`.** Это единственное место, где я knowingly оставил поведение неправильным, потому что исправление ломает чужой тест.
