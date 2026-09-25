# Handoff: scheduler

**Требования:** F15 (расписания, бюджеты, уведомления), частично F14 (потребление бюджета refill'ем), MASTER-PROMPT §7 п. 13, п. 19, п. 20
**Контракт:** `docs/integration/CONTRACTS.ru.md` §3.3 (миграция 7), §5.4 (коды ошибок), §5.5 (единицы), §5.6 (события) — версия 1
**База:** ветка `integration/ultra-2026-09-25`, модуль написан 25 сентября 2026 года поверх HEAD того дня. `db.py` на момент написания **не существует** (`ls proxy_workbench/db.py` пусто), поэтому проверки схемы ведутся на копии DDL из §3.3.

## 1. Прошу внести в чужие файлы

### 1.1 `proxy_workbench/db.py` — миграция 7 (владелец `db.py`)

Миграция 7 в `CONTRACTS.ru.md:244` создаёт `schedules(id, pool_id, kind, interval_minutes, window_json, timezone, quiet_hours_json, budgets_json, next_run_at, enabled)` и `schedule_run(id, schedule_id, started_at, finished_at, state, counters_json)`. Этих колонок не хватает моему модулю; недостающие перечислены константой `scheduler.REQUESTED_COLUMNS` (`proxy_workbench/scheduler.py:1756`).

Прошу добавить в **миграцию 7** (не в новую миграцию — таблицы ещё нигде не созданы):

```sql
-- schedules
last_run_at          REAL,     -- якорь интервальной сетки; без него перезапуск теряет сетку
paused               INTEGER NOT NULL DEFAULT 0,
pause_reason         TEXT,     -- paused_user | paused_quiet_hours | paused_battery | paused_metered
resume_at            REAL,     -- когда тихие часы кончатся
dst_policy           TEXT,     -- skip | shift
catch_up             INTEGER NOT NULL DEFAULT 0,
max_catch_up         INTEGER NOT NULL DEFAULT 1,
wake_gap_s           REAL,     -- 0 = авто (4 интервала, для слотов 1 час)
power_json           TEXT,     -- {on_battery, battery_min_percent, metered, unknown_metered}
notify_json          TEXT,     -- {enter_below, exit_above, min_events, repeat_window_s, reset_after_s, alert_on_unknown}
notifications_json   TEXT,     -- {in_app, os_notifications, email:{label,enabled,user_confirmed}, webhook:{...}}
counters_json        TEXT,     -- накопленные requests/bytes/seconds/reserved/active/relay_*
-- schedule_run
reason               TEXT,     -- due | coalesced_no_catchup | coalesced_after_sleep | manual
missed               INTEGER NOT NULL DEFAULT 0,
```

Почему именно здесь: без `last_run_at` и `counters_json` перезапуск процесса обнуляет интервальную сетку и дневной бюджет, то есть пользователь может превысить лимит простым перезапуском. Модуль это **переживает**: `SqliteScheduleStore` использует колонку, если она есть, и не падает, если её нет (`persists_runtime_state`, `persists_counters` — проверяется в `tests/test_scheduler_store.py`).

Что именно делает модуль с этими колонками (готово, проверяйте по этим вызовам):
- `SqliteScheduleStore.save_spec` — пишет спецификацию в `schedules` явным списком колонок.
- `SqliteScheduleStore.save_state` / `load_state` — `last_run_at`, `next_run_at`, `paused`, `pause_reason`, `resume_at`, `counters_json`.
- `SqliteScheduleStore.append_run` — `schedule_run` плюс `reason` и `missed`, когда колонки есть.

DDL не пишет никто, кроме `db.py`: в `scheduler.py` нет ни одного `CREATE`/`ALTER` (проверяется тестом `test_the_module_writes_no_ddl`).

### 1.2 `proxy_workbench/proxytool.py` (интегратор) — точка подключения

`watch`-цикл (`proxytool.py:2500-2511`) — единственное сегодняшнее «расписание». Прошу заменить его на вызов моего `Scheduler`:

```python
scheduler = scheduler_mod.Scheduler(store=scheduler_mod.SqliteScheduleStore(connection),
                                    notifier=...)
# раз в N секунд, в существующем watch-цикле:
for decision, runs in ((d, r) for d, r in zip(report.decisions, report.run_requests)):
    ...
report = scheduler.tick()                       # -> TickReport
for run in report.run_requests:                 # run.run_id идемпотентен
    start_scan(job_id=run.run_id, pool_id=run.pool_id, budgets=run.budgets, stages=run.stages)
```

Что важно не сломать: `run.run_id = make_run_id(schedule_id, scheduled_for)` — детерминированный, повторный тик даёт тот же id (F29 приёмка №3, `tests/test_scheduler_plans.py::IntervalPlanTests::test_run_id_is_derived_so_a_replay_is_idempotent`).

### 1.3 `proxy_workbench/pools.py` — потребление бюджета и уведомлений

1. Пул тратит бюджет через `BudgetLedger.try_reserve(traffic_class, requests=, bytes=, seconds=)`, а не через собственный счётчик:

```python
ledger = scheduler.Budgets-ledger          # scheduler.Scheduler.ledger(pool_schedule_id, now=now)
reservation = ledger.try_reserve(scheduler.PROBE, requests=1, bytes=body_size, active=True)
if not reservation.ok:
    stop_with_reason(reservation.denial.reason, reservation.denial.to_dict())
...
ledger.commit(reservation, requests=1, bytes=actual)   # или ledger.release(reservation)
```

`traffic_class` — одна из констант `PROBE/SOURCE/RETRY/JUDGE/SPEEDTEST/RELAY`. Relay-счётчики ведутся отдельно и никогда не уменьшают рабочий бюджет (проверяется `tests/test_scheduler_budgets.py::TrafficClassTests`).

2. Пул отдаёт своё здоровье моему `StateWatcher`, а уведомления собирает мой `Notifier`:

```python
watcher = scheduler.StateWatcher(f'pool:{pool_id}', scheduler.NotifyPolicy(enter_below=minimum, exit_above=minimum + 1))
change = watcher.observe(healthy_count, now)
if change is not None:
    notifier.for_change(change, watcher.policy)     # десять одинаковых деградаций -> одно уведомление
```

Гистерезис и dedup уже реализованы; `pools.py` не должен слать уведомления сам.

## 2. Что уже сделано у меня

Публичный API `proxy_workbench/scheduler.py` (leaf-модуль, без сети, без DDL, без файлов):

| Что | Имя | Заметка |
| --- | --- | --- |
| Окно по стенным часам | `Window.parse('09:00-18:00', weekdays=...)`, `.contains`, `.intervals_on`, `.next_open`, `.current_close` | окно через полночь понимает дни недели той даты, с которой открылось |
| Ежедневный слот | `Slot.parse('09:00')`, `.instant_on(tz, date, dst_policy)`, `next_slots(...)` | один момент на дату: fold не удваивает, gap пропускается или сдвигается |
| Спецификация | `ScheduleSpec`, `ScheduleSpec.from_dict/to_dict` | `kind ∈ {interval, slots}`; неизвестное поле → `E_VALIDATION_UNKNOWN_FIELD` |
| Бюджет | `Budgets`, `BudgetCounters`, `BudgetLedger.try_reserve/commit/release/snapshot`, `LimitView`, `BudgetSnapshot` | in-flight конечен и виден: `in_flight`, `allowance`, `exhausted` |
| Питание/сеть | `PowerSignal`, `PowerPolicy`, `power_decision`, `read_system_signal` | без сигнала ОС политика не применяется; `unknown_metered` по умолчанию консервативен |
| Уведомления | `NotifyPolicy`, `StateWatcher`, `Notifier`, `NotificationConfig`, `Target`, `Dispatcher`, `Channel` | внешние каналы молчат без `enabled` + `user_confirmed` |
| Планировщик | `Scheduler.add/get/list/remove/state/ledger/tick/pause/resume/run_now/blocked_by/mark_wake/report` | `tick()` ничего не запускает, только возвращает `RunRequest` |
| Хранилище | `ScheduleStore` (Protocol), `InMemoryScheduleStore`, `SqliteScheduleStore` | свой DDL не пишет ни один |

Коды решений (`REASON_*`) и коды уведомлений (`NOTIFY_*`) — закрытые списки, экспортируются наружу, чтобы UI переводил их через существующий `i18n.tr` (тексты лежат в `scheduler.MESSAGES`).

## 3. Совместимость

- **Что ломается, если не внести 1.1:** после перезапуска процесса теряются интервальная сетка (`last_run_at`) и накопленный бюджет (`counters_json`). Без `paused`/`pause_reason` причина остановки после перезапуска неизвестна. Конкретный сценарий: дневной лимит 100 запросов, израсходовано 100, пользователь перезапустил приложение — лимит снова 100. Ничего не падает, но лимит не работает.
- **Что НЕ ломается:** новый модуль, его не импортирует ни один существующий файл; `proxytool.py`, `api.py`, `gui.py`, `ui/*` не тронуты; чужие тесты не затронуты. Полный набор тестов этой задачей не запускался и не должен запускаться (HANDOFF §5).
- **Схема:** пока миграция 7 не расширена, `SqliteScheduleStore` работает на текущих колонках; `schedules.enabled` используется как есть, `next_run_at` пишется, а `last_run_at` восстанавливается из последней строки `schedule_run` (это единственные данные контракта, которые для этого есть).

## 4. Проверки

Команды (из корня репозитория, `.venv/bin/python`), все пять прогонялись по отдельности и одним вызовом:

```
.venv/bin/python -m unittest tests.test_scheduler_windows        -> Ran 21 tests ... OK
.venv/bin/python -m unittest tests.test_scheduler_budgets        -> Ran 25 tests ... OK
.venv/bin/python -m unittest tests.test_scheduler_notifications -> Ran 19 tests ... OK
.venv/bin/python -m unittest tests.test_scheduler_plans         -> Ran 35 tests ... OK
.venv/bin/python -m unittest tests.test_scheduler_store         -> Ran 16 tests ... OK
.venv/bin/python -m unittest tests.test_scheduler_windows tests.test_scheduler_budgets \
    tests.test_scheduler_notifications tests.test_scheduler_plans tests.test_scheduler_store
                                                              -> Ran 116 tests ... OK
```

Что покрыто поведением, а не текстом: fold/gap в `Europe/Berlin` и `America/Santiago`, quiet hours с авто-возобновлением, объединение просроченных работ после сна, `catch_up` с ограничением `max_catch_up`, in-flight резерв бюджета, разделение relay и рабочего трафика, дедупликация десяти одинаковых деградаций и второе уведомление при восстановлении, молчание внешних каналов без подтверждённой пользователем цели, round-trip спецификации и run history через SQLite, отсутствие canary-секрета в дампе базы.

Непрочитанным осталось: полный `unittest discover -s tests` (запрещён этой задачей и выполняется интегратором), `packaging/smoke.py`, поведение на реальном `pmset`/`/sys/class/power_supply` (проверено на подставных входах), запуск в GUI/CLI (модуль к ним не подключён).

## 5. Открытые вопросы

1. **Кто владеет циклом тикания.** `Scheduler.tick()` должен вызываться существующим watch-циклом или новым сервисом `jobs.py`. Я не выбирал: это решение интегратора, и оба варианта проходят тесты.
2. **`desired`/квоты пула остаются в `pools.py`.** Я считаю только «сколько можно потратить», а не «сколько адресов нужно».
3. **Уведомления в GUI.** `Notifier.sent` отдаёт `Notification.to_dict()`; формат показа в `ui/app.js` — зона поверхности `web`, я её не трогал. Тексты кодов лежат в `scheduler.MESSAGES` и переводятся существующим `i18n.tr`.
4. **F22 (sleep/wake).** `Scheduler.mark_wake()` — готовая точка входа; кто вызывает её (диспетчер питания, tray, детектор сна) — зона `desktop.py`.
